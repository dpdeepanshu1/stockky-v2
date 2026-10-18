"""entry_engine/order_flow.py - order-flow numbers recorded on every entry decision (group 427). RECORD ONLY.

WHY: the entry path already has spread / book-depth guards, but nothing in it reads which SIDE of the book is heavier, and no depth number has
ever been backtested (daily candles cannot test it). So the only honest route is to store the numbers on each decision for a couple of weeks and
then ask, on your own trades, whether entries into a bid-heavy / ask-heavy / wide book did better or worse (trade_records by_imbalance_5,
by_imbalance_20, by_spread). Until that evidence exists nothing here may block, size or order anything.

WHAT IS STORED (decision.flow_json, compact JSON, every value None-safe):
  spread_pct, book_value_5, bid_qty_5, ask_qty_5, imbalance_5 = (bid - ask) / (bid + ask) over the best five levels (+1 all bids, -1 all asks),
  vs_vwap_pct, vs_open_pct, day_pos (0 = at the day low, 1 = at the day high), day_change_pct, volume;
  and, for an approved entry, d20 = {imbalance_20, book_value_20, bid_qty_20, ask_qty_20, buy_impact_pct (cost of buying the planned qty), buy_complete}.
NOT ORDER FLOW: there is no trade-by-trade feed here, so cumulative volume delta / aggressor volume / absorption are NOT recorded (the exchange quote
carries none). imbalance is a snapshot of resting orders, which can be spoofed; treat it as a hypothesis until the report says otherwise.

Every function is safe to call: they never raise and return None / leave the decision untouched on any problem.
"""
from __future__ import annotations

import json
import logging
import math
import time
from typing import Any, Dict, Optional, Tuple

import config

logger = logging.getLogger("real-trade-order-flow")

VERSION = 1
_D20_KEYS = ("imbalance_20", "book_value_20", "bid_qty_20", "ask_qty_20", "buy_impact_pct", "buy_complete", "spread_pct")
_cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
_stats: Dict[str, int] = {"snapshots": 0, "snapshot_empty": 0, "d20_ok": 0, "d20_fail": 0, "d20_cache_hit": 0}


def _f(v) -> Optional[float]:
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


def snapshot_dict(tick) -> Optional[Dict[str, Any]]:
    """The flow numbers a tick already carries, as a dict (None when the tick holds none of them). Pure, never raises."""
    try:
        px = _f(getattr(tick, "price", None))
        vwap, opn = _f(getattr(tick, "vwap", None)), _f(getattr(tick, "open_price", None))
        hi, lo, prev = _f(getattr(tick, "day_high", None)), _f(getattr(tick, "day_low", None)), _f(getattr(tick, "prev_close", None))
        d: Dict[str, Any] = {
            "v": VERSION,
            "spread_pct": _r(_f(getattr(tick, "spread_pct", None)), 4),
            "book_value_5": _r(_f(getattr(tick, "book_value_5", None)), 0),
            "bid_qty_5": _r(_f(getattr(tick, "bid_qty_5", None)), 0),
            "ask_qty_5": _r(_f(getattr(tick, "ask_qty_5", None)), 0),
            "imbalance_5": imbalance(getattr(tick, "bid_qty_5", None), getattr(tick, "ask_qty_5", None)),
            "vs_vwap_pct": _r((px - vwap) / vwap * 100.0, 3) if px and vwap and vwap > 0 else None,
            "vs_open_pct": _r((px - opn) / opn * 100.0, 3) if px and opn and opn > 0 else None,
            "day_pos": _r((px - lo) / (hi - lo), 3) if px is not None and hi is not None and lo is not None and hi > lo else None,
            "day_change_pct": _r((px - prev) / prev * 100.0, 3) if px and prev and prev > 0 else None,
            "volume": getattr(tick, "volume", None),
        }
        if all(d[k] is None for k in ("spread_pct", "book_value_5", "imbalance_5", "vs_vwap_pct", "vs_open_pct", "day_pos")):
            return None
        return d
    except Exception:  # noqa: BLE001
        return None


def record(decision, tick) -> bool:
    """Put the tick's flow numbers on the decision (decision.flow_json). Never raises; False when nothing was recorded."""
    try:
        if not getattr(config, "ORDER_FLOW_RECORD", True):
            return False
        d = snapshot_dict(tick)
        if d is None:
            _stats["snapshot_empty"] += 1
            return False
        decision.flow_json = json.dumps(d, separators=(",", ":"))
        _stats["snapshots"] += 1
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("order-flow record failed: %s", e)
        return False


def d20_subset(book: Any) -> Optional[Dict[str, Any]]:
    """The few 20-level fields worth keeping from market-data's /depth/{symbol} body; None when the book is not available. Pure."""
    if not isinstance(book, dict) or not book.get("available"):
        return None
    out = {k: book.get(k) for k in _D20_KEYS if book.get(k) is not None}
    return out or None


async def fetch_d20(symbol: str, qty: Optional[float]) -> Optional[Dict[str, Any]]:
    """One short /depth/{symbol} call (cached 20 s per symbol, a failure cached 60 s). None on any problem. Never raises."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    now = time.monotonic()
    hit = _cache.get(sym)
    if hit is not None and hit[0] > now:
        _stats["d20_cache_hit"] += 1
        return hit[1]
    out, ttl = None, 60.0
    try:
        import httpx
        from market_feed.feed import MARKET_DATA_URL
        params = {"wait_s": min(1.0, float(config.ORDER_FLOW_DEPTH20_TIMEOUT_S))}
        if qty and qty > 0:
            params["qty"] = float(qty)
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{MARKET_DATA_URL}/depth/{sym}", params=params, timeout=float(config.ORDER_FLOW_DEPTH20_TIMEOUT_S))
        if r.status_code == 200:
            out = d20_subset(r.json())
            if out is not None:
                ttl = 20.0
    except Exception as e:  # noqa: BLE001
        logger.debug("order-flow depth20 %s failed (left out): %s", sym, e)
    _stats["d20_ok" if out is not None else "d20_fail"] += 1
    _cache[sym] = (now + ttl, out)
    if len(_cache) > 1000:
        for k in [k for k, v in _cache.items() if v[0] <= now][:500]:
            _cache.pop(k, None)
    return out


async def add_d20(decision, symbol: str, qty: Optional[float] = None) -> bool:
    """For an approved entry: merge the 20-level book summary into decision.flow_json under "d20". Never raises, never blocks the entry
    longer than ORDER_FLOW_DEPTH20_TIMEOUT_S. False when nothing was added."""
    try:
        if not (getattr(config, "ORDER_FLOW_RECORD", True) and getattr(config, "ORDER_FLOW_DEPTH20", True)):
            return False
        d20 = await fetch_d20(symbol, qty)
        if d20 is None:
            return False
        try:
            base = json.loads(getattr(decision, "flow_json", None) or "{}")
            if not isinstance(base, dict):
                base = {}
        except (TypeError, ValueError):
            base = {"v": VERSION}
        base["d20"] = d20
        decision.flow_json = json.dumps(base, separators=(",", ":"))
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("order-flow add_d20 failed: %s", e)
        return False


def parse(flow_json) -> Dict[str, Any]:
    """flow_json text -> flat dict {imbalance_5, imbalance_20, spread_pct, ...}; {} when absent / unreadable. Pure."""
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


def imbalance_bucket(v) -> str:
    """Bucket for the expectancy groups: ask_heavy (< -0.2) | balanced | bid_heavy (> 0.2) | unknown. Pure."""
    x = _f(v)
    if x is None:
        return "unknown"
    return "ask_heavy" if x < -0.2 else ("bid_heavy" if x > 0.2 else "balanced")


def spread_bucket(v) -> str:
    """tight (< 0.15 %) | normal (< 0.30 %) | wide | unknown. Pure."""
    x = _f(v)
    if x is None:
        return "unknown"
    return "tight" if x < 0.15 else ("normal" if x < 0.30 else "wide")


def stats() -> Dict[str, int]:
    return dict(_stats)


def _reset_for_tests() -> None:
    _cache.clear()
    for k in _stats:
        _stats[k] = 0
