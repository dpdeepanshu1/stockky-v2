"""
services/market-data-service/angelone_scrip_master.py  §1 — symbol-to-token lookup.

AngelOne's REST and WebSocket APIs address every instrument by a numeric
`token` plus its exchange segment — never by trading symbol directly.
angelone_ws_feed.py cannot fetch a single quote without this mapping.
Mirrors the same problem execution/dhan_client.py::get_security_id()
already solves for Dhan, using AngelOne's own official, publicly
documented scrip master file (no auth required, refreshed daily):
https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json

VERIFY this URL still resolves in your environment before relying on it —
AngelOne has moved this file's path in the past. Override via
ANGELONE_SCRIP_MASTER_URL if it has changed.
"""
from __future__ import annotations
import logging
import os
import threading
import time
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("angelone-scrip-master")

SCRIP_MASTER_URL = os.getenv(
    "ANGELONE_SCRIP_MASTER_URL",
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
)
# The file itself is only republished once a day — no point re-fetching more often.
REFRESH_INTERVAL_S = float(os.getenv("ANGELONE_SCRIP_MASTER_REFRESH_S", str(24 * 3600)))

_lock = threading.Lock()
_token_map: Dict[str, str] = {}   # e.g. "SBIN" -> "3045"
_loaded_at: float = 0.0


def _clean(symbol: str) -> str:
    return (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()


def _load_sync() -> None:
    global _token_map, _loaded_at
    try:
        resp = httpx.get(SCRIP_MASTER_URL, timeout=30.0)
        resp.raise_for_status()
        rows = resp.json()
        new_map: Dict[str, str] = {}
        for row in rows:
            # NSE cash-equity rows are suffixed "-EQ" (e.g. "SBIN-EQ") —
            # strip it to match the plain symbols used everywhere else in
            # this codebase (symbol_master, candidate_engine, etc).
            sym_field = str(row.get("symbol", ""))
            if row.get("exch_seg") == "NSE" and sym_field.endswith("-EQ") and row.get("token"):
                new_map[sym_field[:-3].upper()] = str(row["token"])
        if new_map:
            with _lock:
                _token_map = new_map
                _loaded_at = time.time()
            logger.info("AngelOne scrip master loaded: %d NSE-EQ symbols", len(new_map))
        else:
            logger.warning(
                "AngelOne scrip master fetch returned 0 usable NSE-EQ rows — "
                "check ANGELONE_SCRIP_MASTER_URL / the file's schema hasn't changed"
            )
    except Exception as e:
        logger.error("AngelOne scrip master fetch failed: %s", e)


def ensure_loaded() -> None:
    """Load on first use, and refresh once the cached copy is a day old.
    A failed refresh keeps serving the last good map rather than clearing it."""
    if not _token_map or (time.time() - _loaded_at) > REFRESH_INTERVAL_S:
        _load_sync()


def get_token(symbol: str) -> Optional[str]:
    ensure_loaded()
    return _token_map.get(_clean(symbol))


def get_tokens_bulk(symbols: List[str]) -> Dict[str, str]:
    """Returns {clean_symbol: token} — only for symbols actually resolved.
    Silently drops anything not found; callers should log the gap between
    requested and resolved counts if they need visibility into misses."""
    ensure_loaded()
    out: Dict[str, str] = {}
    for s in symbols:
        clean = _clean(s)
        tok = _token_map.get(clean)
        if tok:
            out[clean] = tok
    return out


def status() -> dict:
    return {
        "loaded_symbols": len(_token_map),
        "loaded_at": _loaded_at or None,
        "age_seconds": (time.time() - _loaded_at) if _loaded_at else None,
        "source_url": SCRIP_MASTER_URL,
    }
