"""fo_eod.py - round 554 (E579, Tredye plan P5): the end-of-day F&O pack - OI Flow and futures basis - from the exchange's F&O bhavcopy.
DISPLAY ONLY. Nothing here trades, no switch or Market Weather dial reads it, the F&O engine is not touched. Standard library only (csv), no pandas.

One file (BhavCopy_NSE_FO_0_0_0_<yyyymmdd>_F_0000.csv.zip, the current "UDiFF" layout) holds, per contract: close, previous close, underlying close, open
interest and the change in open interest. From the stock-futures rows (FinInstrmTp == STF) that gives, per stock:

  OI Flow (the four-way rule, same words as analytics.buildup / training.fo_features):
      price up   + OI up   = long build-up      price up   + OI down = short covering
      price down + OI up   = short build-up     price down + OI down = long unwinding        flat price or flat OI = no clear read
    price = the NEAR-month future's close against its own previous close; OI = open interest summed over ALL listed expiries (so a roll from the near
    to the next month, which moves OI between months, does not read as a build-up or an unwinding).
  Basis: near-month future close against the underlying's close in the same file (UndrlygPric): premium / discount %, annualised carry
    (premium % x 365 / days to expiry), and the next-month share of OI as a rollover read.

Rules (plan section 4): every number has a date (the file's own trade date), a missing number is None (the page draws a dash), never 0. A file whose trade
date is not the date asked for is refused. A day with no file says so and the last good day stays on screen with its own date.
Layout check: the columns are matched by name; a file missing a needed column is reported by name, not guessed at. UNVERIFIED against a live file (no network
where this was written): the column names below are the exchange's published UDiFF names from memory - run `python3 fo_eod.py <file.zip|file.csv>` on the VM
with a real file first; it prints what it parsed.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger("market-data-fo-eod")

IST = ZoneInfo("Asia/Kolkata")
BHAV_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
_SYM = re.compile(r"^[A-Z0-9&\-]{1,20}$")

# needed columns (lower-case): a file without any of these is refused with the names listed
REQUIRED = ("traddt", "tckrsymb", "fininstrmtp", "xprydt", "clspric", "prvsclsgpric", "opnintrst", "chnginopnintrst")
# optional: without it the basis is None for every stock, the OI flow still works
OPTIONAL = ("undrlygpric",)

BUILDUPS = ("long_buildup", "short_covering", "short_buildup", "long_unwinding")
# the exchange publishes the day's file in the evening; before this IST hour the expected session is the one before
PUBLISH_HOUR_IST = 19
NEAR_EXPIRY_DAYS = 5      # inside this, the rollover share is flagged "rolling"
MAX_ROWS = 400


def _num(v: Any) -> Optional[float]:
    """float or None. Blank, '-', 'NA' and anything not finite are None; 0 is kept as 0 here (callers decide whether 0 is usable)."""
    try:
        s = str(v).replace(",", "").strip()
        if not s or s.upper() in ("-", "NA", "N/A", "NAN"):
            return None
        f = float(s)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pos(v: Any) -> Optional[float]:
    f = _num(v)
    return f if f is not None and f > 0 else None


def _date(v: Any) -> Optional[date]:
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------------------------------------------- parse
def parse_contracts(text: str) -> Dict[str, Any]:
    """{ok, trade_date, contracts: [..], missing_columns, rows_seen} from a bhavcopy body. contracts = stock-futures rows only, one dict per expiry:
    symbol, expiry (date), close, prev_close, spot, oi, oi_chg. Never raises. `ok` is False when a required column is missing or no stock-futures row
    was found; `trade_date` is the file's own TradDt (None when the rows disagree or none parse)."""
    out: Dict[str, Any] = {"ok": False, "trade_date": None, "contracts": [], "missing_columns": [], "rows_seen": 0}
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", errors="replace")
    if not text:
        return out
    if text.startswith("﻿"):
        text = text[1:]
    try:
        reader = csv.reader(io.StringIO(text))
        header = next(reader)
    except (StopIteration, csv.Error):
        return out
    idx = {str(h).strip().lower(): i for i, h in enumerate(header)}
    miss = [c for c in REQUIRED if c not in idx]
    if miss:
        out["missing_columns"] = miss
        return out
    i_sym, i_kind, i_exp = idx["tckrsymb"], idx["fininstrmtp"], idx["xprydt"]
    i_cls, i_prv, i_oi, i_chg, i_td = idx["clspric"], idx["prvsclsgpric"], idx["opnintrst"], idx["chnginopnintrst"], idx["traddt"]
    i_und = idx.get("undrlygpric")
    top = max(i_sym, i_kind, i_exp, i_cls, i_prv, i_oi, i_chg, i_td, i_und if i_und is not None else 0)
    dates = set()
    try:
        for row in reader:
            out["rows_seen"] += 1
            if len(row) <= top or row[i_kind].strip().upper() != "STF":
                continue
            sym = row[i_sym].strip().upper()
            exp = _date(row[i_exp])
            td = _date(row[i_td])
            if not _SYM.match(sym) or exp is None or td is None:
                continue
            dates.add(td)
            out["contracts"].append({
                "symbol": sym, "expiry": exp, "close": _pos(row[i_cls]), "prev_close": _pos(row[i_prv]),
                "spot": _pos(row[i_und]) if i_und is not None else None, "oi": _num(row[i_oi]), "oi_chg": _num(row[i_chg]),
            })
    except csv.Error:
        pass
    if len(dates) == 1:
        out["trade_date"] = next(iter(dates))
    out["ok"] = bool(out["contracts"]) and out["trade_date"] is not None
    return out


def classify(price_chg_pct: Optional[float], oi_chg_pct: Optional[float]) -> Optional[str]:
    """The four-way rule. None when either input is missing or either is exactly flat (no clear read - never forced into a box)."""
    if price_chg_pct is None or oi_chg_pct is None or price_chg_pct == 0 or oi_chg_pct == 0:
        return None
    if price_chg_pct > 0:
        return "long_buildup" if oi_chg_pct > 0 else "short_covering"
    return "short_buildup" if oi_chg_pct > 0 else "long_unwinding"


def _r(v: Optional[float], nd: int = 2) -> Optional[float]:
    return None if v is None else round(v, nd)


def build_rows(contracts: List[Dict[str, Any]], trade_date: date) -> List[Dict[str, Any]]:
    """One row per stock. Fields (None = unknown, shown as a dash): near_expiry, days_to_expiry, fut_close, price_chg_pct, oi, oi_chg, oi_chg_pct, buildup,
    spot, basis_pct, annualised_pct, roll_pct (next-month share of the OI over the first three expiries), rolling (inside NEAR_EXPIRY_DAYS)."""
    by_sym: Dict[str, List[Dict[str, Any]]] = {}
    for c in contracts:
        if c["expiry"] >= trade_date:
            by_sym.setdefault(c["symbol"], []).append(c)
    rows: List[Dict[str, Any]] = []
    for sym, cs in by_sym.items():
        cs.sort(key=lambda c: c["expiry"])
        near = cs[0]
        days = (near["expiry"] - trade_date).days
        # OI over all expiries; a missing OI or change on any expiry makes the total unknown rather than partial
        ois = [c["oi"] for c in cs]
        chgs = [c["oi_chg"] for c in cs]
        oi = sum(ois) if all(o is not None for o in ois) else None
        oi_chg = sum(chgs) if all(g is not None for g in chgs) else None
        oi_prev = (oi - oi_chg) if (oi is not None and oi_chg is not None) else None
        oi_chg_pct = (oi_chg / oi_prev * 100.0) if (oi_prev is not None and oi_prev > 0) else None
        px = ((near["close"] / near["prev_close"] - 1.0) * 100.0) if (near["close"] and near["prev_close"]) else None
        spot = near["spot"]
        basis = ((near["close"] - spot) / spot * 100.0) if (near["close"] and spot) else None
        ann = (basis * 365.0 / days) if (basis is not None and days >= 1) else None
        roll = None
        if len(cs) >= 2 and all(c["oi"] is not None for c in cs[:3]):
            tot3 = sum(c["oi"] for c in cs[:3])
            roll = (cs[1]["oi"] / tot3 * 100.0) if tot3 > 0 else None
        if oi is not None and oi <= 0:
            continue                                  # no open interest at all: nothing to read
        rows.append({
            "symbol": sym, "near_expiry": near["expiry"].isoformat(), "days_to_expiry": days, "fut_close": _r(near["close"]),
            "price_chg_pct": _r(px), "oi": _r(oi, 0), "oi_chg": _r(oi_chg, 0), "oi_chg_pct": _r(oi_chg_pct),
            "buildup": classify(px, oi_chg_pct), "spot": _r(spot), "basis_pct": _r(basis, 3), "annualised_pct": _r(ann, 1),
            "roll_pct": _r(roll, 1), "rolling": days <= NEAR_EXPIRY_DAYS,
        })
    rows.sort(key=lambda r: r["symbol"])
    return rows[:MAX_ROWS]


def build_pack(text: Any, want_date: Optional[date] = None) -> Dict[str, Any]:
    """{ok, trade_date (ISO), rows, counts, error?}. `want_date` set and different from the file's own date = refused (never shows another day's file as
    the day asked for). Never raises."""
    p = parse_contracts(text)
    if p["missing_columns"]:
        return {"ok": False, "error": "layout: missing column(s) " + ", ".join(p["missing_columns"]), "rows": []}
    if not p["ok"]:
        return {"ok": False, "error": "no stock-futures rows found in the file", "rows": []}
    td: date = p["trade_date"]
    if want_date is not None and td != want_date:
        return {"ok": False, "error": f"file is dated {td.isoformat()}, not {want_date.isoformat()}", "rows": []}
    rows = build_rows(p["contracts"], td)
    counts = {b: sum(1 for r in rows if r["buildup"] == b) for b in BUILDUPS}
    counts["stocks"] = len(rows)
    counts["no_basis"] = sum(1 for r in rows if r["basis_pct"] is None)
    return {"ok": True, "trade_date": td.isoformat(), "rows": rows, "counts": counts}


# ---------------------------------------------------------------------------------------------------------------- sessions
def is_session_day(d: date) -> bool:
    """Mon-Fri and not on the system's NSE holiday list (market_hours, the same list the feeds use)."""
    if d.weekday() >= 5:
        return False
    try:
        import market_hours
        return not market_hours.is_nse_holiday_ist(datetime(d.year, d.month, d.day, 12, 0))
    except Exception:  # noqa: BLE001 - no list available: weekdays count, a missing file then says so
        return True


def candidate_sessions(now: Optional[datetime] = None, n: int = 6) -> List[date]:
    """Newest first: the session whose file should be out (today after PUBLISH_HOUR_IST on a session day, else the last completed session), then earlier ones."""
    now = (now or datetime.now(timezone.utc)).astimezone(IST)
    d = now.date()
    if not (is_session_day(d) and now.hour >= PUBLISH_HOUR_IST):
        d -= timedelta(days=1)
    out: List[date] = []
    guard = 0
    while len(out) < n and guard < 40:
        if is_session_day(d):
            out.append(d)
        d -= timedelta(days=1)
        guard += 1
    return out


# ---------------------------------------------------------------------------------------------------------------- fetch + cache
_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {"good": None, "good_at": 0.0, "tried_at": 0.0, "error": None, "last_url_status": None}
_SNAPSHOT_PATH = (os.getenv("FO_EOD_CACHE_PATH") or "").strip() or os.path.join(tempfile.gettempdir(), "market_data_fo_eod.json")


def _enabled() -> bool:
    return (os.getenv("FO_EOD_ENABLED") or "").strip().lower() not in ("0", "false", "no", "off")


def _cfg() -> Tuple[float, float]:
    """(ttl_s when the newest expected file is already held, retry_s while it is still missing). FO_EOD_TTL_S default 6 h, FO_EOD_RETRY_S default 15 min."""
    def _f(name: str, dflt: float) -> float:
        try:
            v = float((os.getenv(name) or "").strip() or dflt)
        except ValueError:
            return dflt
        return v if math.isfinite(v) and v >= 60 else dflt
    return _f("FO_EOD_TTL_S", 6 * 3600.0), _f("FO_EOD_RETRY_S", 900.0)


def _body_from_download(content: bytes) -> Optional[str]:
    if not content:
        return None
    if content[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                name = next((n for n in z.namelist() if n.lower().endswith(".csv")), None)
                return z.read(name).decode("utf-8", errors="replace") if name else None
        except zipfile.BadZipFile:
            return None
    head = content[:400].decode("utf-8", errors="replace").lower()
    if "<html" in head or "access denied" in head:      # an interstitial / block page, not data
        return None
    return content.decode("utf-8", errors="replace")


def _download(d: date) -> Optional[str]:
    """The day's file text, or None (404 on a holiday / not yet published, a block page, a network failure). Uses the process's cached NSE session."""
    import bhavcopy
    client = bhavcopy._nse_client()
    try:
        r = client.get(BHAV_URL.format(yyyymmdd=d.strftime("%Y%m%d")))
    except Exception as e:  # noqa: BLE001
        _STATE["last_url_status"] = type(e).__name__
        return None
    _STATE["last_url_status"] = r.status_code
    return _body_from_download(r.content) if r.status_code == 200 else None


def _save_snapshot(pack: Dict[str, Any]) -> None:
    try:
        d = os.path.dirname(_SNAPSHOT_PATH) or "."
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(pack, f)
        os.replace(tmp, _SNAPSHOT_PATH)
    except Exception as e:  # noqa: BLE001
        logger.debug("fo-eod snapshot not saved: %s", e)


def _load_snapshot() -> Optional[Dict[str, Any]]:
    try:
        with open(_SNAPSHOT_PATH) as f:
            p = json.load(f)
        return p if isinstance(p, dict) and p.get("ok") and p.get("trade_date") and isinstance(p.get("rows"), list) else None
    except Exception:  # noqa: BLE001
        return None


def reset() -> None:
    with _LOCK:
        _STATE.update(good=None, good_at=0.0, tried_at=0.0, error=None, last_url_status=None)


def get_pack(now: Optional[datetime] = None) -> Dict[str, Any]:
    """The pack for the route: {enabled, expected_date, trade_date, stale, rows, counts, fetched_at, error?}. Walks the candidate sessions newest first and
    keeps the first good file; the last good one stays when a refresh fails (stale = it is older than the expected session). One caller fetches at a
    time; the others get what is held. Never raises."""
    now_utc = now or datetime.now(timezone.utc)
    cands = candidate_sessions(now_utc)
    expected = cands[0].isoformat() if cands else None
    if not _enabled():
        return {"enabled": False, "expected_date": expected, "trade_date": None, "stale": False, "rows": [], "counts": {}}
    ttl, retry = _cfg()
    with _LOCK:                                         # decide and claim the fetch under the lock; do the slow download outside it
        good = _STATE["good"]
        if good is None:
            good = _STATE["good"] = _load_snapshot()    # after a restart: held but possibly old
        have_expected = bool(good) and good.get("trade_date") == expected
        tried = _STATE["tried_at"]
        age = (time.monotonic() - tried) if tried else None
        need = age is None or age >= (ttl if have_expected else retry)
        if need:
            _STATE["tried_at"] = time.monotonic()       # other callers now see a fresh attempt and just read what is held
    if need:
        err = None
        got = None
        held_date = good.get("trade_date") if good else None
        for d in cands:
            if held_date and d.isoformat() <= held_date:
                break                                   # nothing newer than what is held can be found below here
            try:
                body = _download(d)
            except Exception as e:  # noqa: BLE001
                body, err = None, type(e).__name__
            if body is None:
                continue
            pack = build_pack(body, d)
            if pack.get("ok"):
                got = pack
                break
            err = pack.get("error")
        with _LOCK:
            if got:
                got["fetched_at"] = now_utc.astimezone(IST).isoformat(timespec="seconds")
                _STATE.update(good=got, good_at=time.monotonic(), error=None)
                good = got
            else:
                _STATE["error"] = err or ("no newer file yet" if good else "no file found for the recent sessions")
        if got:
            _save_snapshot(got)
    with _LOCK:
        err_now = _STATE["error"]
        good = _STATE["good"]
    out: Dict[str, Any] = {"enabled": True, "expected_date": expected, "rows": [], "counts": {}, "trade_date": None, "stale": False}
    if good:
        out.update(trade_date=good["trade_date"], rows=good["rows"], counts=good.get("counts", {}), fetched_at=good.get("fetched_at"),
                   stale=bool(expected and good["trade_date"] < expected))
    if err_now and (not good or out["stale"]):
        out["error"] = err_now
    return out


if __name__ == "__main__":        # hand check on the VM: python3 fo_eod.py <file.zip | file.csv>
    import sys
    raw = open(sys.argv[1], "rb").read() if len(sys.argv) > 1 else b""
    txt = _body_from_download(raw) or ""
    res = build_pack(txt)
    print({k: v for k, v in res.items() if k != "rows"})
    for r in res.get("rows", [])[:5]:
        print(r)
