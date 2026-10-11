"""feed/fno_list.py - group305: which NSE stocks have stock futures (the "F&O" badge / filter on the Market Board).

NO separate F&O universe: the board ranks the same equity universe and only tags + filters. Source: Dhan's public instrument
file (SEM_INSTRUMENT_NAME == FUTSTK; underlying = trading symbol minus the "-Mon2026-FUT" tail), streamed row by row, refreshed
daily in a background thread and snapshotted to disk. Unreachable / unexpected shape -> a built-in list of well-known F&O names
(badge approximate, not absent). FNO_SYMBOLS_EXTRA / FNO_SYMBOLS_EXCLUDE adjust it.
UNVERIFIED against the live file (no network where this was written): run check A from the plan / docs on the VM.
Nothing here raises into a caller or touches the network on a request path."""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import tempfile
import threading
import time
from typing import Iterable, Iterator, Optional, Set

logger = logging.getLogger("position-stocks-fno")

_CACHE_PATH = (os.getenv("FNO_LIST_CACHE_PATH") or "").strip() or "/tmp/position_stocks_fno_list.json"
_REFRESH_S = 24 * 3600.0
_MAX_DOWNLOAD_S = 120.0
MIN_PLAUSIBLE = 50
_lock = threading.Lock()
_loaded: Optional[Set[str]] = None
_loaded_at = 0.0
_refreshing = False
_status = {"source": "builtin", "count": 0, "last_error": None}

_UNDERLYING_RE = re.compile(r"-(?:[A-Z]{3}-?\d{4}|\d{1,2}-?[A-Z]{3}-?\d{4}|[A-Z]{3}\d{2}).*$")

_BUILTIN = frozenset("""
AARTIIND ABB ABBOTINDIA ABCAPITAL ABFRL ACC ADANIENT ADANIPORTS ALKEM AMBUJACEM ANGELONE APOLLOHOSP APOLLOTYRE ASHOKLEY
ASIANPAINT ASTRAL ATGL AUBANK AUROPHARMA AXISBANK BAJAJ-AUTO BAJAJFINSV BAJFINANCE BALKRISIND BANDHANBNK BANKBARODA BANKINDIA
BEL BERGEPAINT BHARATFORG BHARTIARTL BHEL BIOCON BOSCHLTD BPCL BRITANNIA BSE BSOFT CANBK CDSL CESC CGPOWER CHAMBLFERT CHOLAFIN
CIPLA COALINDIA COFORGE COLPAL CONCOR COROMANDEL CROMPTON CUMMINSIND CYIENT DABUR DALBHARAT DEEPAKNTR DELHIVERY DIVISLAB DIXON
DLF DMART DRREDDY EICHERMOT ESCORTS EXIDEIND FEDERALBNK GAIL GLENMARK GMRAIRPORT GODREJCP GODREJPROP GRANULES GRASIM HAL
HAVELLS HCLTECH HDFCAMC HDFCBANK HDFCLIFE HEROMOTOCO HINDALCO HINDCOPPER HINDPETRO HINDUNILVR HUDCO ICICIBANK ICICIGI ICICIPRULI
IDEA IDFCFIRSTB IEX IGL IIFL INDHOTEL INDIANB INDIGO INDUSINDBK INDUSTOWER INFY IOC IRB IRCTC IREDA IRFC ITC JINDALSTEL JIOFIN
JSWENERGY JSWSTEEL JUBLFOOD KALYANKJIL KEI KOTAKBANK KPITTECH LAURUSLABS LICHSGFIN LICI LODHA LT LTF LTIM LTTS LUPIN
M&M M&MFIN MANAPPURAM MARICO MARUTI MAXHEALTH MAZDOCK MCX METROPOLIS MFSL MGL MOTHERSON MPHASIS MRF MUTHOOTFIN NATIONALUM
NAUKRI NBCC NCC NESTLEIND NHPC NMDC NTPC NYKAA OBEROIRLTY OFSS OIL ONGC PAGEIND PATANJALI PAYTM PEL PERSISTENT PETRONET PFC
PIDILITIND PIIND PNB PNBHOUSING POLICYBZR POLYCAB POONAWALLA POWERGRID PRESTIGE RAMCOCEM RBLBANK RECLTD RELIANCE SAIL SAMMAANCAP
SBICARD SBILIFE SBIN SHREECEM SHRIRAMFIN SIEMENS SJVN SOLARINDS SONACOMS SRF SUNPHARMA SUPREMEIND SUZLON SYNGENE TATACHEM
TATACOMM TATACONSUM TATAELXSI TATAMOTORS TATAPOWER TATASTEEL TATATECH TCS TECHM TIINDIA TITAGARH TITAN TORNTPHARM TORNTPOWER
TRENT TVSMOTOR UNIONBANK UNITDSPR UNOMINDA UPL ULTRACEMCO VBL VEDL VOLTAS WIPRO YESBANK ZYDUSLIFE
""".split())


def _split_env(name: str) -> Set[str]:
    return {x.strip().upper() for x in (os.getenv(name) or "").split(",") if x.strip()}


_COMPACT_COLS = ("SEM_EXM_EXCH_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL")
_DETAILED_COLS = ("EXCH_ID", "INSTRUMENT", "UNDERLYING_SYMBOL")
_SANE_NAME = re.compile(r"^[A-Z0-9&][A-Z0-9&\-]{0,24}$")


def parse_report(lines: Iterable[str]) -> dict:
    """group311: parse a Dhan instrument file and say what was found. Understands the compact layout (SEM_EXM_EXCH_ID /
    SEM_INSTRUMENT_NAME / SEM_TRADING_SYMBOL, underlying = trading symbol minus the expiry tail) and the detailed layout (EXCH_ID /
    INSTRUMENT / UNDERLYING_SYMBOL; column names from memory, UNVERIFIED). Returns {shape, rows, futstk_rows, underlyings, sample,
    suspicious, header}; shape is "unknown" when neither column set is present. Pure; no network."""
    out = {"shape": "unknown", "rows": 0, "futstk_rows": 0, "underlyings": set(), "sample": [], "suspicious": [], "header": []}
    reader = csv.reader(lines)
    try:
        header = next(reader)
    except StopIteration:
        return out
    header = [h.strip().lstrip("\ufeff") for h in header]
    out["header"] = header[:40]
    idx = {h: i for i, h in enumerate(header)}
    if all(k in idx for k in _COMPACT_COLS):
        out["shape"] = "compact"
        i_ex, i_ins, i_sym = (idx[k] for k in _COMPACT_COLS)
        i_und = None
    elif all(k in idx for k in _DETAILED_COLS):
        out["shape"] = "detailed"
        i_ex, i_ins, i_und = (idx[k] for k in _DETAILED_COLS)
        i_sym = idx.get("SYMBOL_NAME", idx.get("DISPLAY_NAME", i_und))
    else:
        return out
    top = max(i_ex, i_ins, i_sym, i_und if i_und is not None else 0)
    for row in reader:
        out["rows"] += 1
        if len(row) <= top:
            continue
        if row[i_ex].strip().upper() != "NSE" or row[i_ins].strip().upper() != "FUTSTK":
            continue
        out["futstk_rows"] += 1
        raw = row[i_sym].strip().upper()
        sym = row[i_und].strip().upper() if i_und is not None else ""
        if not sym:
            sym = _UNDERLYING_RE.sub("", raw)
        if not sym:
            continue
        if len(out["sample"]) < 5:
            out["sample"].append({"raw": raw[:40], "underlying": sym})
        if not _SANE_NAME.match(sym) and len(out["suspicious"]) < 10:
            out["suspicious"].append(sym)
        out["underlyings"].add(sym)
    return out


def parse_fut_underlyings(lines: Iterable[str]) -> Set[str]:
    return parse_report(lines)["underlyings"]


def _save_snapshot(symbols: Set[str]) -> None:
    try:
        d = os.path.dirname(_CACHE_PATH) or "."
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump({"saved_at": time.time(), "symbols": sorted(symbols)}, f)
        os.replace(tmp, _CACHE_PATH)
    except Exception as e:  # noqa: BLE001
        logger.debug("fno list snapshot not saved: %s", e)


def _load_snapshot() -> Optional[Set[str]]:
    try:
        with open(_CACHE_PATH) as f:
            data = json.load(f)
        syms = {str(s).upper() for s in data.get("symbols", [])}
        return syms if len(syms) >= MIN_PLAUSIBLE else None
    except Exception:  # noqa: BLE001
        return None


def reset() -> None:
    global _loaded, _loaded_at, _refreshing
    with _lock:
        _loaded = None; _loaded_at = 0.0; _refreshing = False
        _status.update(source="builtin", count=0, last_error=None)


def _base_set() -> Set[str]:
    global _loaded, _loaded_at
    with _lock:
        if _loaded is None:
            snap = _load_snapshot()
            if snap:
                _loaded, _loaded_at = snap, time.time()
                _status.update(source="snapshot", count=len(snap))
        base = set(_loaded) if _loaded else set(_BUILTIN)
        if not _loaded:
            _status.update(source="builtin", count=len(base))
    return base


def get_fno_symbols() -> Set[str]:
    try:
        out = _base_set() | _split_env("FNO_SYMBOLS_EXTRA")
        return out - _split_env("FNO_SYMBOLS_EXCLUDE")
    except Exception:  # noqa: BLE001
        return set(_BUILTIN)


def is_fno(symbol: str) -> bool:
    return str(symbol or "").upper().replace(".NS", "").replace(".BO", "").strip() in get_fno_symbols()


def status() -> dict:
    get_fno_symbols()
    with _lock:
        return dict(_status, loaded_at=_loaded_at or None)


def _download(report: bool = False):
    import config
    import httpx
    t0 = time.time()
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        with c.stream("GET", config.FNO_LIST_URL) as r:
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")

            def gen() -> Iterator[str]:
                for line in r.iter_lines():
                    if time.time() - t0 > _MAX_DOWNLOAD_S:
                        raise RuntimeError("download too slow")
                    yield line
            return parse_report(gen()) if report else parse_fut_underlyings(gen())


def diagnose(report: Optional[dict] = None) -> dict:
    """group311 (check A): what the Dhan instrument file really contains versus the built-in list. Downloads the file unless a parsed
    `report` is passed (tests). Never changes the loaded set."""
    try:
        rep = report if report is not None else _download(report=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "verdict": f"could not download the Dhan instrument file ({type(e).__name__}: {str(e)[:120]}) - the F&O badge uses the built-in list",
                "source_now": status().get("source")}
    und = set(rep["underlyings"])
    miss = sorted(_BUILTIN - und)
    if rep["shape"] == "unknown":
        verdict = f"file layout not recognised (columns: {', '.join(rep['header'][:8])}...) - parser needs a new mapping"
    elif len(und) < MIN_PLAUSIBLE:
        verdict = f"only {len(und)} FUTSTK underlyings found in {rep['futstk_rows']} FUTSTK rows - not plausible, built-in list stays in use"
    elif rep["suspicious"]:
        verdict = f"{len(und)} underlyings but some names look wrong ({', '.join(rep['suspicious'][:5])}) - check the tail-stripping pattern"
    else:
        verdict = f"ok: {len(und)} F&O underlyings parsed ({rep['shape']} layout)"
    return {"ok": rep["shape"] != "unknown" and len(und) >= MIN_PLAUSIBLE and not rep["suspicious"], "verdict": verdict,
            "shape": rep["shape"], "rows": rep["rows"], "futstk_rows": rep["futstk_rows"], "underlyings": len(und),
            "sample": rep["sample"], "suspicious": rep["suspicious"], "in_builtin_not_in_file": miss[:40],
            "in_file_not_in_builtin": len(und - _BUILTIN), "source_now": status().get("source")}


def refresh(force: bool = False) -> bool:
    global _loaded, _loaded_at, _refreshing
    with _lock:
        if _refreshing or (not force and _loaded_at and time.time() - _loaded_at < _REFRESH_S):
            return False
        _refreshing = True
    try:
        syms = _download()
        if len(syms) < MIN_PLAUSIBLE:
            raise RuntimeError(f"only {len(syms)} FUTSTK underlyings found - unexpected file shape")
        with _lock:
            _loaded, _loaded_at = syms, time.time()
            _status.update(source="dhan", count=len(syms), last_error=None)
        _save_snapshot(syms)
        logger.info("position-stocks F&O list: %d underlyings from the Dhan instrument file", len(syms))
        return True
    except Exception as e:  # noqa: BLE001
        with _lock:
            _status["last_error"] = f"{type(e).__name__}: {e}"
        logger.info("position-stocks F&O list refresh failed (%s: %s) - using %s", type(e).__name__, e, _status["source"])
        return False
    finally:
        with _lock:
            _refreshing = False


def refresh_in_background() -> None:
    try:
        import config
        if not getattr(config, "FNO_LIST_REFRESH", True):
            return
        threading.Thread(target=refresh, name="fno-list-refresh", daemon=True).start()
    except Exception as e:  # noqa: BLE001
        logger.debug("fno refresh not started: %s", e)
