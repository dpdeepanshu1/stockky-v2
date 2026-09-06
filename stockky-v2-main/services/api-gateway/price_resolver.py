"""
Unified safe price resolution for Market Scan / Lite Scan / Surprise.

Frontend may bind: close | price | cmp | current_price | ltp | last_price | prev_close
Every scan row should set all of these to the same positive float when known.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


_TICK_KEYS = (
    "price",
    "cmp",
    "last_price",
    "close",
    "ltp",
    "regularMarketPrice",
    "last",
    "current_price",
)
_DECISION_KEYS = (
    "close",
    "price",
    "cmp",
    "current_price",
    "ltp",
    "last_price",
)
_FEED_KEYS = (
    "close",
    "price",
    "cmp",
    "ltp",
    "prev_close",
    "last",
    "current_price",
)


def _as_positive_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        px = float(val)
    except (TypeError, ValueError):
        return None
    if px <= 0 or px != px:  # NaN
        return None
    return round(px, 2)


def extract_safe_price(
    symbol: str = "",
    tick: Optional[dict] = None,
    feed: Optional[dict] = None,
    decision: Optional[dict] = None,
) -> float:
    """
    Guarantees a non-negative float price for frontend rendering.
    Priority: live tick → decision payload → data feed / baseline → 0.0
    """
    if isinstance(tick, dict):
        for k in _TICK_KEYS:
            px = _as_positive_float(tick.get(k))
            if px is not None:
                return px

    if isinstance(decision, dict):
        for k in _DECISION_KEYS:
            px = _as_positive_float(decision.get(k))
            if px is not None:
                return px

    if isinstance(feed, dict):
        for k in _FEED_KEYS:
            px = _as_positive_float(feed.get(k))
            if px is not None:
                return px
        # Nested Neon / upstream shapes: metrics, data, quote, ohlc, ticker
        for nest_key in ("metrics", "data", "quote", "ohlc", "ticker", "info"):
            nested = feed.get(nest_key)
            if isinstance(nested, dict):
                for k in _FEED_KEYS:
                    px = _as_positive_float(nested.get(k))
                    if px is not None:
                        return px

    return 0.0


def resolve_display_price(
    symbol: str = "",
    tick: Optional[dict] = None,
    feed: Optional[dict] = None,
    decision: Optional[dict] = None,
) -> float:
    """Alias used by scan / sniper call sites — same as extract_safe_price."""
    return extract_safe_price(symbol=symbol, tick=tick, feed=feed, decision=decision)


def apply_price_aliases(row: Dict[str, Any], price: float) -> Dict[str, Any]:
    """
    Write close/price/cmp/current_price/ltp so any frontend binding works.
    Does not overwrite an existing positive close with 0.
    """
    if not isinstance(row, dict):
        return row
    px = _as_positive_float(price)
    if px is None:
        # Keep existing positive values if any
        existing = extract_safe_price(decision=row, feed=row, tick=row)
        if existing <= 0:
            return row
        px = existing
    row["close"] = px
    row["price"] = px
    row["cmp"] = px
    row["current_price"] = px
    row["ltp"] = px
    row["last_price"] = px
    # Preserve existing prev_close if already a positive baseline; else mirror
    existing_pc = _as_positive_float(row.get("prev_close"))
    if existing_pc is None:
        row["prev_close"] = px
    return row


def ensure_row_price(
    row: Dict[str, Any],
    *,
    tick: Optional[dict] = None,
    feed: Optional[dict] = None,
) -> Dict[str, Any]:
    """Resolve price from row + optional tick/feed and stamp all aliases."""
    if not isinstance(row, dict):
        return row
    px = extract_safe_price(
        symbol=str(row.get("symbol") or ""),
        tick=tick,
        feed=feed,
        decision=row,
    )
    return apply_price_aliases(row, px)
