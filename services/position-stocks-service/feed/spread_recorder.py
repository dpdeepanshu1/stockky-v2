"""feed/spread_recorder.py - E285 round 109: persist each stock's quoted spread, one reading per minute. RECORD ONLY.

WHY: the entry depth gate refuses a name whose spread is above one fixed ceiling (ENTRY_DEPTH_MAX_SPREAD_PCT), the same for every stock. A stock-specific ceiling needs the stock's OWN
spread history by minute of the day, and nothing kept it: the bar recorder keeps open / high / low / close / volume only. This keeps the spread so a later round can learn the
ceiling from it. Nothing in the entry path reads these files.

WHERE THE SPREAD COMES FROM: the market-data quote row's `spread_pct` (Dhan's best-5 book; a percentage of the price). It is read from the bulk rows the poller already fetches
(feed/md_poller.ingest_rows) and from every /quote the depth gate asks for (orders/depth_gate._fetch). A row without a valid `spread_pct` is not counted (never a guess). If the bulk
rows carry none, only the depth gate's candidates are recorded: stats() `rows_seen` / `rows_with_spread` show which it is.

LAYOUT  <BAR_RECORD_DIR>/spread/<YYYY-MM-DD>/<quote(SYMBOL)>.json
  {"v": 1, "symbol", "date", "n", "rows": [[t, median_pct, max_pct, samples], ...]}   t = epoch seconds of the minute. The folder sits beside the bar day folders, NOT inside one, so the
  bar recorder's own day listing, pruning and the volume-profile read never see it.
HOW: note() keeps the readings of the current minute in memory (at most SPREAD_RECORD_MAX_SAMPLES per symbol-minute, at most SPREAD_RECORD_MAX_SYMBOLS symbols a day); run_once(now,
market_open) is called by screening/board_runner next to the bar recorder, writes at most every SPREAD_RECORD_EVERY_S (a file only when its row count changed), makes one final flush after
15:30, merges with the file on disk the first time this process writes a symbol (a minute in memory wins) and deletes day folders older than SPREAD_RECORD_KEEP_DAYS once per day.
SAFE: note() never raises and never blocks (a lock and a dict); every public function swallows and counts its own errors; an unwritable directory switches writing off with the reason in stats().
Switch SPREAD_RECORD_ENABLED.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

logger = logging.getLogger("position-stocks-spread-recorder")

IST_OFFSET_S = 330 * 60
MAX_SANE_PCT = 25.0                      # a spread above this is a bad reading, not a spread
_DAY_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_lock = threading.Lock()
_cur: Dict[str, List[Any]] = {}          # SYMBOL -> [minute_start, [readings]] for the minute being filled
_done: Dict[str, Dict[float, List[float]]] = {}      # SYMBOL -> {minute_start: [t, median, max, n]} finished minutes of `_day`
_written: Dict[str, tuple] = {}          # SYMBOL -> (date, rows, last t) as last written
_day: Dict[str, Optional[str]] = {"d": None}
_state: Dict[str, Any] = {"rows_seen": 0, "rows_with_spread": 0, "rejected": 0, "dropped_symbols": 0, "last_run": 0.0, "last_write": 0.0, "final_day": None, "pruned_day": None,
                          "files_written": 0, "merged_existing": 0, "errors": 0, "last_error": None, "disabled_reason": None}


def _cfg(name: str, default: Any) -> Any:
    try:
        import config
        return getattr(config, name, default)
    except Exception as e:  # noqa: BLE001
        logger.debug("spread recorder: config %s unreadable (%s)", name, e)
        return default


def enabled() -> bool:
    return bool(_cfg("SPREAD_RECORD_ENABLED", True))


def reset() -> None:
    with _lock:
        _cur.clear()
        _done.clear()
        _written.clear()
        _day["d"] = None
        _state.update(rows_seen=0, rows_with_spread=0, rejected=0, dropped_symbols=0, last_run=0.0, last_write=0.0, final_day=None, pruned_day=None, files_written=0, merged_existing=0,
                      errors=0, last_error=None, disabled_reason=None)


def ist_date(ts: float) -> str:
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=float(ts) + IST_OFFSET_S)).strftime("%Y-%m-%d")


def _clean_symbol(symbol: Any) -> str:
    return str(symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()


def _median(xs: List[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _finish_locked(sym: str) -> None:
    """Turn the symbol's finished current minute into a row. The caller holds `_lock`."""
    cur = _cur.get(sym)
    if not cur or not cur[1]:
        return
    vals = cur[1]
    _done.setdefault(sym, {})[cur[0]] = [cur[0], round(_median(vals), 4), round(max(vals), 4), len(vals)]


def _roll_day_locked(ts: float) -> None:
    d = ist_date(ts)
    if _day["d"] != d:
        _cur.clear()
        _done.clear()
        _written.clear()
        _day["d"] = d


def note(symbol: Any, spread_pct: Any, ts: Optional[float] = None) -> bool:
    """Count one spread reading (a percentage of the price) for `symbol` at epoch seconds `ts` (default now). True when it was kept. Never raises, never blocks, no I/O."""
    try:
        if not enabled():
            return False
        ts = time.time() if ts is None else float(ts)
        sym = _clean_symbol(symbol)
        if not sym or sym.startswith("^"):
            return False
        with _lock:
            _state["rows_seen"] += 1
            try:
                v = float(spread_pct)
            except (TypeError, ValueError):
                return False
            if not (v == v and 0.0 < v <= MAX_SANE_PCT):
                _state["rejected"] += 1 if spread_pct is not None else 0
                return False
            _state["rows_with_spread"] += 1
            _roll_day_locked(ts)
            minute = float(int(ts // 60) * 60)
            cur = _cur.get(sym)
            if cur is None:
                if len(_cur) >= int(_cfg("SPREAD_RECORD_MAX_SYMBOLS", 600)):
                    _state["dropped_symbols"] += 1
                    return False
                _cur[sym] = [minute, [v]]
                return True
            if minute < cur[0]:                      # a late reading for a minute already closed: ignored
                return False
            if minute > cur[0]:
                _finish_locked(sym)
                _cur[sym] = [minute, [v]]
                return True
            if len(cur[1]) < int(_cfg("SPREAD_RECORD_MAX_SAMPLES", 30)):
                cur[1].append(v)
            return True
    except Exception as e:  # noqa: BLE001
        logger.debug("spread recorder: note failed (%s)", e)
        return False


def note_row(row: Any, ts: Optional[float] = None) -> bool:
    """note() from a market-data quote row ({"symbol", "spread_pct", ...}). Never raises."""
    try:
        if not isinstance(row, dict):
            return False
        return note(row.get("symbol"), row.get("spread_pct"), ts)
    except Exception as e:  # noqa: BLE001
        logger.debug("spread recorder: note_row failed (%s)", e)
        return False


def file_for(root: str, date: str, symbol: str) -> str:
    return os.path.join(root, "spread", date, quote(symbol, safe="") + ".json")


def _err(where: str, e: Exception) -> None:
    _state["errors"] += 1
    _state["last_error"] = f"{where}: {type(e).__name__}: {str(e)[:120]}"
    logger.warning("spread recorder %s failed (%s: %s)", where, type(e).__name__, e)


def _read(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _clean_rows(rows: Any) -> List[List[float]]:
    out: Dict[float, List[float]] = {}
    for r in rows or []:
        try:
            t, med, mx, n = (float(x) for x in list(r)[:4])
        except (TypeError, ValueError):
            continue
        if t > 0 and 0 < med <= MAX_SANE_PCT and med <= mx <= MAX_SANE_PCT and n >= 1:
            out[t] = [t, round(med, 4), round(mx, 4), int(n)]
    return [out[k] for k in sorted(out)]


def _atomic_write(path: str, doc: Dict[str, Any]) -> int:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return os.path.getsize(path)


def write_symbol(root: str, date: str, symbol: str, rows: List[List[float]]) -> Optional[int]:
    """Write one symbol's day (merged with the file on disk the first time this process writes it). Bytes written, or None when nothing changed. Raises only OSError-like problems."""
    mem = _clean_rows(rows)
    if not mem:
        return None
    path = file_for(root, date, symbol)
    prev = _written.get(symbol)
    merged = False
    if prev is None or prev[0] != date:
        old = _read(path)
        if old and isinstance(old.get("rows"), list):
            disk = _clean_rows(old["rows"])
            by_t = {r[0]: r for r in disk}
            by_t.update({r[0]: r for r in mem})                 # a minute in memory wins
            mem = [by_t[k] for k in sorted(by_t)]
            merged = bool(disk)
    sig = (date, len(mem), mem[-1][0], mem[-1][3])
    if prev == sig:
        return None
    size = _atomic_write(path, {"v": 1, "symbol": symbol, "date": date, "n": len(mem), "rows": mem})
    _written[symbol] = sig
    if merged:
        _state["merged_existing"] += 1
    return size


def prune_days(root: str, keep_days: int, today: str) -> int:
    """Delete <root>/spread/<date> folders older than keep_days. Anything not shaped like a date folder is left alone."""
    removed = 0
    base = os.path.join(root, "spread")
    try:
        cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=int(keep_days))).strftime("%Y-%m-%d")
        for name in os.listdir(base):
            p = os.path.join(base, name)
            if _DAY_DIR.match(name) and name < cutoff and os.path.isdir(p):
                for fn in os.listdir(p):
                    try:
                        os.unlink(os.path.join(p, fn))
                    except OSError as e:
                        logger.debug("spread recorder: could not delete %s (%s)", fn, e)
                try:
                    os.rmdir(p)
                    removed += 1
                except OSError as e:
                    logger.debug("spread recorder: could not remove %s (%s)", p, e)
    except (OSError, ValueError) as e:
        logger.debug("spread recorder: prune skipped (%s)", e)
    return removed


def _after_close(now: float) -> bool:
    ist = datetime.fromtimestamp(now, tz=timezone(timedelta(hours=5, minutes=30)))
    m = ist.hour * 60 + ist.minute
    return ist.weekday() < 5 and 15 * 60 + 30 <= m <= 16 * 60 + 30


def run_once(now: float, market_open: bool) -> Dict[str, Any]:
    """One writer pass (market open: at most every SPREAD_RECORD_EVERY_S; once more after the close). Never raises."""
    did: Dict[str, Any] = {"wrote": 0, "skipped": None, "final": False, "pruned": 0}
    try:
        if not enabled():
            did["skipped"] = "disabled"
            return did
        if _state["disabled_reason"]:
            did["skipped"] = "disabled_reason"
            return did
        final = (not market_open) and _after_close(now)
        if not market_open and not final:
            did["skipped"] = "closed"
            return did
        root = str(_cfg("BAR_RECORD_DIR", "/data/bars"))
        with _lock:
            if final and _state["final_day"] == ist_date(now):
                did["skipped"] = "final_done"
                return did
            if not final and now - _state["last_run"] < float(_cfg("SPREAD_RECORD_EVERY_S", 300)):
                did["skipped"] = "interval"
                return did
            _state["last_run"] = now
            date = _day["d"]
            if date is None:
                did["skipped"] = "nothing_noted"
                return did
            snap: Dict[str, List[List[float]]] = {}
            for sym in set(_cur) | set(_done):
                if final:
                    _finish_locked(sym)                      # the last minute is closed at the close
                rows = list(_done.get(sym, {}).values())
                if rows:
                    snap[sym] = rows
        try:
            os.makedirs(os.path.join(root, "spread"), exist_ok=True)
            if not os.access(root, os.W_OK):
                raise PermissionError(f"{root} is not writable")
        except OSError as e:
            _state["disabled_reason"] = f"{root}: {type(e).__name__}: {str(e)[:100]}"
            logger.warning("spread recorder switched off: %s", _state["disabled_reason"])
            did["skipped"] = "unwritable"
            return did
        for sym, rows in snap.items():
            try:
                if write_symbol(root, date, sym, rows) is not None:
                    did["wrote"] += 1
                    _state["files_written"] += 1
            except Exception as e:  # noqa: BLE001
                _err(f"write {sym}", e)
        if did["wrote"]:
            _state["last_write"] = now
        if final:
            _state["final_day"] = ist_date(now)
            did["final"] = True
        today = ist_date(now)
        if _state["pruned_day"] != today:
            _state["pruned_day"] = today
            did["pruned"] = prune_days(root, int(_cfg("SPREAD_RECORD_KEEP_DAYS", 45)), today)
    except Exception as e:  # noqa: BLE001
        _err("run_once", e)
    return did


def history(symbol: str, days: int = 20, root: Optional[str] = None, before_date: Optional[str] = None) -> List[List[float]]:
    """Recorded spread rows [t, median, max, samples] of `symbol` over the last `days` day folders (ascending), for the round that will learn the ceiling. Never raises."""
    try:
        root = root or str(_cfg("BAR_RECORD_DIR", "/data/bars"))
        base = os.path.join(root, "spread")
        names = sorted((n for n in os.listdir(base) if _DAY_DIR.match(n) and (before_date is None or n < before_date)), reverse=True)
        out: List[List[float]] = []
        for n in names[:max(0, int(days))]:
            doc = _read(file_for(root, n, _clean_symbol(symbol)))
            if doc and isinstance(doc.get("rows"), list):
                out = _clean_rows(doc["rows"]) + out
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("spread recorder: history %s failed (%s)", symbol, e)
        return []


def stats() -> Dict[str, Any]:
    """For /board/status. Reads the directory listing only. Never raises."""
    root = str(_cfg("BAR_RECORD_DIR", "/data/bars"))
    days: List[str] = []
    last_files = 0
    try:
        days = sorted(n for n in os.listdir(os.path.join(root, "spread")) if _DAY_DIR.match(n))
        if days:
            last_files = sum(1 for n in os.listdir(os.path.join(root, "spread", days[-1])) if n.endswith(".json"))
    except OSError as e:
        logger.debug("spread recorder: stats listing skipped (%s)", e)
    with _lock:
        s = dict(_state)
        s["symbols_open"] = len(_cur)
    return {"enabled": enabled(), "dir": os.path.join(root, "spread"), **s, "days_on_disk": len(days), "first_day": days[0] if days else None,
            "last_day": days[-1] if days else None, "last_day_files": last_files}
