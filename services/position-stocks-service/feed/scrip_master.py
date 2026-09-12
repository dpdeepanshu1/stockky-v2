"""
feed/scrip_master.py — AngelOne symbol→token map for position-stocks-service.

Duplicated from market-data-service/angelone_scrip_master.py (same
isolation rationale — see config.py docstring). Provides both:
  - get_token(symbol) → AngelOne numeric token (for WS subscribe/data)
  - get_all_nse_eq() → full {symbol: token} dict (for building the
    subscription list at WS startup)

No auth required — AngelOne's scrip master JSON is a public file.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional

import httpx

import config

logger = logging.getLogger("position-stocks-scrip-master")

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
REFRESH_INTERVAL_S = 24 * 3600

_lock = threading.Lock()
_token_map: Dict[str, str] = {}   # clean_symbol -> AngelOne numeric token
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
            sym_field = str(row.get("symbol", ""))
            if row.get("exch_seg") == "NSE" and sym_field.endswith("-EQ") and row.get("token"):
                new_map[sym_field[:-3].upper()] = str(row["token"])
        if new_map:
            with _lock:
                _token_map = new_map
                _loaded_at = time.time()
            logger.info("position-stocks: scrip master loaded: %d NSE-EQ symbols", len(new_map))
        else:
            logger.warning("position-stocks: scrip master returned 0 usable NSE-EQ rows")
    except Exception as e:
        logger.error("position-stocks: scrip master fetch failed: %s", e)


def ensure_loaded() -> None:
    if not _token_map or (time.time() - _loaded_at) > REFRESH_INTERVAL_S:
        _load_sync()


def get_token(symbol: str) -> Optional[str]:
    ensure_loaded()
    return _token_map.get(_clean(symbol))


def get_all_nse_eq() -> Dict[str, str]:
    """Full {clean_symbol: token} map — used to build the WS subscription
    list at startup when SCAN_UNIVERSE_SOURCE=all_nse_eq."""
    ensure_loaded()
    return dict(_token_map)


def get_tokens_bulk(symbols: List[str]) -> Dict[str, str]:
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
