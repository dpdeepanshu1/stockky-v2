"""Dhan depth check before an entry (group 280, plan Phase C3).

The scalper's own AngelOne feed only carries the best bid / ask. market-data-service /quote carries Dhan's 5-level book
(`spread_pct`, `book_value_5` = both sides, Rs). This asks it once, right before capital is reserved, and refuses a name
whose spread is too wide or whose book is too thin. Fails OPEN on everything: no answer, a timeout, a bad body, a quote
without depth. Never raises. Answers are cached for a few seconds so one cycle does not ask twice for the same symbol.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Optional

import config

logger = logging.getLogger("position-stocks-depth-gate")

_CACHE_TTL_S = 5.0
_cache: dict = {}


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f >= 0 else None


def _fetch(symbol: str) -> Optional[dict]:
    now = time.time()
    hit = _cache.get(symbol)
    if hit and now - hit[0] < _CACHE_TTL_S:
        return hit[1]
    import httpx
    with httpx.Client(timeout=config.ENTRY_DEPTH_TIMEOUT_S) as client:
        r = client.get(f"{config.MARKET_DATA_URL}/quote/{symbol}")
    body = r.json() if r.status_code == 200 else None
    body = body if isinstance(body, dict) else None
    _cache[symbol] = (now, body)
    return body


def reject_reason(symbol: str) -> Optional[str]:
    """A skip reason ("DEPTH_SPREAD:..." / "DEPTH_THIN_BOOK:...") or None when the entry may go ahead."""
    try:
        if not config.ENTRY_DEPTH_GATE:
            return None
        max_spread = float(config.ENTRY_DEPTH_MAX_SPREAD_PCT or 0)
        min_book = float(config.ENTRY_MIN_BOOK_VALUE or 0)
        if max_spread <= 0 and min_book <= 0:
            return None
        q = _fetch(symbol)
        if not q:
            return None
        spread = _num(q.get("spread_pct"))
        book = _num(q.get("book_value_5"))
        src = q.get("source") or "?"
        if max_spread > 0 and spread is not None and spread > max_spread:
            return f"DEPTH_SPREAD:{spread:.2f}% > {max_spread:.2f}% (src={src})"
        if min_book > 0 and book is not None and book < min_book:
            return f"DEPTH_THIN_BOOK:best-5 book Rs{book:,.0f} < Rs{min_book:,.0f} (src={src})"
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("depth gate %s failed (allowing): %s", symbol, e)
        return None


def max_qty_from_book(symbol: str, ltp: float) -> Optional[int]:
    """group283 (plan C3 size-down): the most shares whose value stays within ENTRY_BOOK_MAX_SHARE_PCT % of one side of
    the best-5 book (book_value_5 / 2). None = no cap (feature off, depth unknown, bad price). Minimum answer 1.
    Fails open and never raises."""
    try:
        share = float(config.ENTRY_BOOK_MAX_SHARE_PCT or 0)
        if not config.ENTRY_DEPTH_GATE or share <= 0 or not ltp or ltp <= 0:
            return None
        q = _fetch(symbol)
        book = _num((q or {}).get("book_value_5"))
        if not book or book <= 0:
            return None
        return max(1, int(book / 2.0 * share / 100.0 / float(ltp)))
    except Exception as e:  # noqa: BLE001
        logger.debug("book size cap %s failed (no cap): %s", symbol, e)
        return None
