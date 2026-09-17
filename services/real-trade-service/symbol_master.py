"""
symbol_master.py — real-trade-service's local copy of the NSE-EQ symbol
universe (2026-09-17, session56 audit follow-up).

Duplicated from position-stocks-service/feed/scrip_master.py — same
isolation rationale as event_depth_local.py's docstring: real-trade-service
shouldn't have to reach into another service's process just to know whether
"RELIANCE" is a real, currently-listed NSE equity. Source is AngelOne's
public scrip master JSON (no auth required), same source scrip_master.py
already uses and has proven reliable.

Built for watchlist_engine/afterhours_scan.py, which previously validated
RSS-extracted symbol candidates against a ~90-ticker hardcoded whitelist
plus a blind length/stoplist heuristic for everything else — missing real
short tickers (TCS, ITC, SBIN are all <6 chars) and letting through
non-tickers that happened to be long capitalized English words. This module
gives it the real ~2000-symbol NSE-EQ universe to check against instead.

Resilience posture (matches the rest of this codebase):
  - In-memory cache, refreshed every REFRESH_INTERVAL_S (24h) or on first use.
  - A live fetch failure falls back to the last snapshot persisted in
    trade_resilience_cache (resilience/local_cache.py — the same store
    market_feed's ATR cache and watchlist_engine/sources.py's Tier 1 payload
    already use), so a restart during a market-data outage doesn't leave
    the scan with zero symbols to validate against.
  - If BOTH the live fetch and the cached snapshot are empty (e.g. the very
    first run, cold DB, no network), callers degrade to their own fallback
    logic rather than extracting nothing — see afterhours_scan._extract_symbol.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger("real-trade-symbol-master")

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
REFRESH_INTERVAL_S = 24 * 3600
_CACHE_KEY = "nse_symbol_master"

_lock = asyncio.Lock()
_symbols: set[str] = set()
_loaded_at: float = 0.0


def _clean(symbol: str) -> str:
    return (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()


async def _load(db=None) -> None:
    """Try a live fetch first; on failure or empty result, fall back to the
    last snapshot persisted in trade_resilience_cache. Never raises."""
    global _symbols, _loaded_at
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(SCRIP_MASTER_URL)
            resp.raise_for_status()
            rows = resp.json()
        new_symbols: set[str] = set()
        for row in rows:
            sym_field = str(row.get("symbol", ""))
            if row.get("exch_seg") == "NSE" and sym_field.endswith("-EQ"):
                new_symbols.add(sym_field[:-3].upper())
        if new_symbols:
            _symbols = new_symbols
            _loaded_at = time.time()
            logger.info("real-trade: symbol master loaded: %d NSE-EQ symbols", len(new_symbols))
            if db is not None:
                try:
                    from resilience.local_cache import save_snapshot
                    save_snapshot(db, _CACHE_KEY, {"symbols": sorted(new_symbols), "loaded_at": _loaded_at})
                except Exception:
                    logger.exception("real-trade: symbol master snapshot persist failed (non-fatal)")
            return
        logger.warning("real-trade: symbol master live fetch returned 0 usable NSE-EQ rows")
    except Exception as e:
        logger.warning("real-trade: symbol master live fetch failed: %s", e)

    # Live fetch failed or came back empty — fall back to the last good
    # snapshot rather than leaving _symbols empty for a whole scan pass.
    if not _symbols and db is not None:
        try:
            from resilience.local_cache import load_snapshot
            snap = load_snapshot(db, _CACHE_KEY)
            if snap and snap.get("symbols"):
                _symbols = set(snap["symbols"])
                _loaded_at = float(snap.get("loaded_at") or 0.0)
                logger.info(
                    "real-trade: symbol master restored %d symbol(s) from local cache "
                    "(live fetch unavailable this pass)", len(_symbols),
                )
        except Exception:
            logger.exception("real-trade: symbol master cache fallback read failed (non-fatal)")


async def ensure_loaded(db=None) -> None:
    if _symbols and (time.time() - _loaded_at) <= REFRESH_INTERVAL_S:
        return
    async with _lock:
        # Re-check inside the lock — another caller may have just loaded it.
        if not _symbols or (time.time() - _loaded_at) > REFRESH_INTERVAL_S:
            await _load(db)


async def is_valid_symbol(symbol: str, db=None) -> bool:
    await ensure_loaded(db)
    return _clean(symbol) in _symbols


async def get_all_symbols(db=None) -> set[str]:
    """Full set of clean (no .NS/.BO suffix) NSE-EQ symbols. Empty set means
    both the live fetch and the cache fallback came up empty — callers
    should treat that as 'validation unavailable this pass', not 'zero
    valid symbols exist'."""
    await ensure_loaded(db)
    return set(_symbols)


def status() -> dict:
    return {
        "loaded_symbols": len(_symbols),
        "loaded_at": _loaded_at or None,
        "age_seconds": (time.time() - _loaded_at) if _loaded_at else None,
        "source_url": SCRIP_MASTER_URL,
    }
