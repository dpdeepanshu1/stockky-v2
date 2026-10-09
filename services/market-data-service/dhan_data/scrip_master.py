"""dhan_data/scrip_master.py - Dhan's instrument list: NSE symbol -> Dhan numeric securityId (group 270).

Dhan identifies instruments by its own numeric ids, which are NOT AngelOne tokens, so Dhan's public scrip master
CSV (default https://images.dhan.co/api-data/api-scrip-master.csv - VERIFY the URL and column names against
Dhan's current docs) is loaded once a day. Column names are matched case-insensitively, with the compact and the
detailed master spellings both accepted, so a rename of one header does not silently empty the map.

Kept: NSE equity (segment NSE_EQ, instrument EQUITY, series EQ) and index rows (segment IDX_I).
A snapshot is persisted through kv_cache so a restart during a Dhan outage still has a map.
Load never raises; failures leave the previous map in place.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import threading
import time
from typing import Dict, Iterable, Optional

from . import config

logger = logging.getLogger("dhan-data.scrip")

REFRESH_S = 24 * 3600
_KV_KEY = "stockky:dhan_scrip_map"   # prefix registered in kv_cache._DURABLE_PREFIXES
_KV_TTL_S = 3 * 24 * 3600

_lock = threading.Lock()
_equity: Dict[str, int] = {}     # "RELIANCE" -> 2885
_index: Dict[str, int] = {}      # normalised index name -> id
_loaded_at: float = 0.0
_last_error: Optional[str] = None
_loading = False

# Yahoo-style index tickers (what the rest of the service uses) -> names Dhan may list them under.
INDEX_ALIASES: Dict[str, tuple] = {
    "^NSEI": ("NIFTY", "NIFTY 50", "NIFTY50"),
    "^NSEBANK": ("BANKNIFTY", "NIFTY BANK", "BANK NIFTY"),
    "^INDIAVIX": ("INDIA VIX", "INDIAVIX"),
    "^BSESN": ("SENSEX", "BSE SENSEX"),
    "^CNXIT": ("NIFTY IT", "NIFTYIT"),
    "^NSMIDCP": ("NIFTY NEXT 50", "NIFTYNXT50", "NIFTY NEXT50"),
    "^NSEMDCP100": ("NIFTY MIDCAP 100", "NIFTYMIDCAP100"),
    "FINNIFTY": ("FINNIFTY", "NIFTY FIN SERVICE"),
    "MIDCPNIFTY": ("MIDCPNIFTY", "NIFTY MIDCAP SELECT"),
}


def _norm_index(name: str) -> str:
    return "".join(ch for ch in (name or "").upper() if ch.isalnum())


def _pick(row: dict, *names: str) -> str:
    for n in names:
        v = row.get(n)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def parse_csv(text: str) -> tuple[Dict[str, int], Dict[str, int]]:
    """Parse the CSV text into (equity_map, index_map). Pure function (tested offline)."""
    equity: Dict[str, int] = {}
    index: Dict[str, int] = {}
    reader = csv.DictReader(io.StringIO(text))
    for raw in reader:
        row = {(k or "").strip().upper(): v for k, v in raw.items()}
        exch = _pick(row, "SEM_EXM_EXCH_ID", "EXCH_ID")
        seg = _pick(row, "SEM_SEGMENT", "SEGMENT")
        inst = _pick(row, "SEM_INSTRUMENT_NAME", "INSTRUMENT").upper()
        sec_id = _pick(row, "SEM_SMST_SECURITY_ID", "SECURITY_ID")
        if not sec_id:
            continue
        try:
            sid = int(float(sec_id))
        except ValueError:
            continue
        sym = _pick(row, "SEM_TRADING_SYMBOL", "SYMBOL_NAME", "UNDERLYING_SYMBOL").upper()
        series = _pick(row, "SEM_SERIES", "SERIES").upper()
        if exch.upper() == "NSE" and (seg in ("E", "NSE_EQ") or inst == "EQUITY") and inst in ("EQUITY", ""):
            if series and series != "EQ":
                continue
            if sym and sym not in equity:      # first row wins; a later duplicate never overwrites
                equity[sym] = sid
        elif inst == "INDEX" or seg in ("I", "IDX_I"):
            for key in (sym, _pick(row, "SM_SYMBOL_NAME", "SYMBOL_NAME", "SEM_CUSTOM_SYMBOL").upper()):
                k = _norm_index(key)
                if k and k not in index:
                    index[k] = sid
    return equity, index


def _store_snapshot(equity: dict, index: dict) -> None:
    try:
        import kv_cache
        kv_cache.kv_set(_KV_KEY, {"eq": equity, "ix": index}, ttl=_KV_TTL_S)
    except Exception as e:  # noqa: BLE001 - snapshot is best-effort
        logger.debug("dhan scrip snapshot not stored: %s", type(e).__name__)


def _load_snapshot() -> bool:
    global _equity, _index, _loaded_at
    try:
        import kv_cache
        raw = kv_cache.kv_get(_KV_KEY)
        if not raw:
            return False
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        eq = {k: int(v) for k, v in (data.get("eq") or {}).items()}
        ix = {k: int(v) for k, v in (data.get("ix") or {}).items()}
        if not eq:
            return False
        with _lock:
            _equity, _index, _loaded_at = eq, ix, time.time() - REFRESH_S + 3600   # retry the live fetch in ~1 h
        logger.info("dhan scrip master: restored snapshot (%d equities, %d indices)", len(eq), len(ix))
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("dhan scrip snapshot not restored: %s", type(e).__name__)
        return False


def load(force: bool = False) -> bool:
    """Download + parse the master. Returns True when a usable map is in place. Never raises."""
    global _equity, _index, _loaded_at, _last_error, _loading
    with _lock:
        if _loading:
            return bool(_equity)
        if not force and _equity and (time.time() - _loaded_at) < REFRESH_S:
            return True
        _loading = True
    try:
        import httpx
        with httpx.Client(timeout=60.0, follow_redirects=True) as c:
            r = c.get(config.scrip_master_url())
            r.raise_for_status()
            text = r.text
        eq, ix = parse_csv(text)
        if len(eq) < 100:
            raise ValueError(f"scrip master parsed only {len(eq)} NSE equities - header names changed?")
        with _lock:
            _equity, _index, _loaded_at, _last_error = eq, ix, time.time(), None
        _store_snapshot(eq, ix)
        logger.info("dhan scrip master loaded: %d NSE equities, %d indices", len(eq), len(ix))
        return True
    except Exception as e:  # noqa: BLE001
        _last_error = f"{type(e).__name__}: {str(e)[:160]}"
        logger.warning("dhan scrip master load failed (keeping previous map): %s", _last_error)
        if not _equity:
            _load_snapshot()
        return bool(_equity)
    finally:
        with _lock:
            _loading = False


def ensure_loaded(block: bool = True) -> bool:
    """Make sure a map exists. With block=False a missing map only starts a background load."""
    if _equity and (time.time() - _loaded_at) < REFRESH_S:
        return True
    if block:
        return load()
    threading.Thread(target=load, name="dhan-scrip-load", daemon=True).start()
    return bool(_equity)


def _clean(symbol: str) -> str:
    return (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()


def is_index(symbol: str) -> bool:
    s = (symbol or "").strip().upper()
    return s.startswith("^") or s in INDEX_ALIASES


def security_id(symbol: str) -> Optional[tuple[str, int]]:
    """(exchangeSegment, securityId) for a symbol or Yahoo-style index ticker, or None when Dhan has no row."""
    s = (symbol or "").strip().upper()
    if not s:
        return None
    if is_index(s):
        for alias in INDEX_ALIASES.get(s, (s.lstrip("^"),)):
            sid = _index.get(_norm_index(alias))
            if sid is not None:
                return "IDX_I", sid
        return None
    sid = _equity.get(_clean(s))
    return ("NSE_EQ", sid) if sid is not None else None


def reverse_map(segment: str) -> Dict[int, str]:
    """{securityId: symbol} for NSE_EQ (used to map quote responses back). Index ids map to the Yahoo ticker."""
    with _lock:
        if segment == "NSE_EQ":
            return {v: k for k, v in _equity.items()}
        out: Dict[int, str] = {}
        for yahoo, aliases in INDEX_ALIASES.items():
            for a in aliases:
                sid = _index.get(_norm_index(a))
                if sid is not None:
                    out.setdefault(sid, yahoo)
        return out


def split_known(symbols: Iterable[str]) -> tuple[list[str], list[str]]:
    """(symbols Dhan knows, symbols it does not)."""
    known, unknown = [], []
    for s in symbols:
        (known if security_id(s) else unknown).append(s)
    return known, unknown


def status() -> dict:
    with _lock:
        return {
            "equities": len(_equity),
            "indices": len(_index),
            "age_s": round(time.time() - _loaded_at, 0) if _loaded_at else None,
            "last_error": _last_error,
        }


def _set_for_tests(equity: dict, index: dict | None = None) -> None:
    global _equity, _index, _loaded_at
    with _lock:
        _equity, _index, _loaded_at = dict(equity), dict(index or {}), time.time()
