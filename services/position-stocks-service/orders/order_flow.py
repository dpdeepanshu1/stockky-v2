"""orders/order_flow.py - order-flow numbers stored on every new position (group 427). RECORD ONLY.

Same idea and same JSON as real-trade-service entry_engine/order_flow.py: spread, best-5 bid / ask quantity and imbalance, book value, price vs VWAP /
open, day position, and (ORDER_FLOW_DEPTH20) the 20-level imbalance and the cost of buying this size. Stored on scalp_positions.flow_json so that
after about two weeks the entries made into a bid-heavy / ask-heavy / wide book can be compared on the trades' own P&L. Nothing here blocks, sizes
or orders anything. It reads the same cached /quote row the depth, circuit and VWAP gates read (no extra call) plus at most ONE short /depth/{symbol}
call. Every function never raises; an unknown value is left out.
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any, Dict, Optional
from urllib.parse import quote

import config

logger = logging.getLogger("position-stocks-order-flow")

VERSION = 1
_D20_KEYS = ("imbalance_20", "book_value_20", "bid_qty_20", "ask_qty_20", "buy_impact_pct", "buy_complete")


def _f(v) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _r(v: Optional[float], nd: int) -> Optional[float]:
    return None if v is None else round(v, nd)


def imbalance(bid_qty, ask_qty) -> Optional[float]:
    """(bid - ask) / (bid + ask) in [-1, 1]; None when either side is unknown or both are empty. Pure."""
    b, a = _f(bid_qty), _f(ask_qty)
    if b is None or a is None or b < 0 or a < 0 or (b + a) <= 0:
        return None
    return round((b - a) / (b + a), 4)


def snapshot_dict(q: Any, ltp: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Flow numbers from a market-data /quote row. None when the row holds none of them. Pure, never raises."""
    try:
        if not isinstance(q, dict):
            return None
        px = _f(ltp) or _f(q.get("price"))
        vwap, opn = _f(q.get("avg_price")), _f(q.get("open"))
        hi, lo, prev = _f(q.get("day_high")), _f(q.get("day_low")), _f(q.get("previous_close"))
        d: Dict[str, Any] = {
            "v": VERSION,
            "spread_pct": _r(_f(q.get("spread_pct")), 4),
            "book_value_5": _r(_f(q.get("book_value_5")), 0),
            "bid_qty_5": _r(_f(q.get("bid_qty_5")), 0),
            "ask_qty_5": _r(_f(q.get("ask_qty_5")), 0),
            "imbalance_5": imbalance(q.get("bid_qty_5"), q.get("ask_qty_5")),
            "vs_vwap_pct": _r((px - vwap) / vwap * 100.0, 3) if px and vwap and vwap > 0 else None,
            "vs_open_pct": _r((px - opn) / opn * 100.0, 3) if px and opn and opn > 0 else None,
            "day_pos": _r((px - lo) / (hi - lo), 3) if px is not None and hi is not None and lo is not None and hi > lo else None,
            "day_change_pct": _r((px - prev) / prev * 100.0, 3) if px and prev and prev > 0 else None,
            "volume": q.get("volume"),
        }
        if all(d[k] is None for k in ("spread_pct", "book_value_5", "imbalance_5", "vs_vwap_pct", "vs_open_pct", "day_pos")):
            return None
        return d
    except Exception:  # noqa: BLE001
        return None


def d20_subset(book: Any) -> Optional[Dict[str, Any]]:
    """The few 20-level fields worth keeping from market-data /depth/{symbol}; None when the book is not available. Pure."""
    if not isinstance(book, dict) or book.get("available") is not True:
        return None
    out = {k: book.get(k) for k in _D20_KEYS if book.get(k) is not None}
    return out or None


def _fetch_d20(symbol: str, quantity: Optional[int]) -> Optional[Dict[str, Any]]:
    try:
        import httpx
        wait = max(0.0, min(1.0, float(getattr(config, "ENTRY_DEPTH20_WAIT_S", 1.0) or 0.0)))
        timeout = max(0.2, float(getattr(config, "ORDER_FLOW_DEPTH20_TIMEOUT_S", 1.5)))
        params: Dict[str, Any] = {"wait_s": wait}
        if quantity and quantity > 0:
            params["qty"] = quantity
        with httpx.Client(timeout=timeout) as client:
            r = client.get(f"{config.MARKET_DATA_URL}/depth/{quote(str(symbol), safe='&')}", params=params)
        return d20_subset(r.json()) if r.status_code == 200 else None
    except Exception as e:  # noqa: BLE001
        logger.debug("order-flow depth20 %s failed (left out): %s", symbol, e)
        return None


def snapshot_json(symbol: str, ltp: Optional[float] = None, quantity: Optional[int] = None) -> Optional[str]:
    """Compact JSON for scalp_positions.flow_json, or None (switched off, no depth / reference known, any error). Never raises."""
    try:
        if not getattr(config, "ORDER_FLOW_RECORD", True):
            return None
        from orders import depth_gate
        d = snapshot_dict(depth_gate._fresh_quote(symbol), ltp)
        if getattr(config, "ORDER_FLOW_DEPTH20", True):
            d20 = _fetch_d20(symbol, quantity)
            if d20 is not None:
                d = d or {"v": VERSION}
                d["d20"] = d20
        return json.dumps(d, separators=(",", ":")) if d else None
    except Exception as e:  # noqa: BLE001
        logger.debug("order-flow snapshot %s failed: %s", symbol, e)
        return None


def parse(flow_json) -> Dict[str, Any]:
    """flow_json text -> flat dict (d20 fields lifted to the top level); {} when absent / unreadable. Pure."""
    try:
        d = json.loads(flow_json) if isinstance(flow_json, str) and flow_json else {}
        if not isinstance(d, dict):
            return {}
        d20 = d.pop("d20", None)
        if isinstance(d20, dict):
            for k in ("imbalance_20", "book_value_20", "buy_impact_pct"):
                if d20.get(k) is not None:
                    d[k] = d20[k]
        return d
    except (TypeError, ValueError):
        return {}
