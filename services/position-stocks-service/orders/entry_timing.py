"""orders/entry_timing.py - E251 (2026-10-28): do not buy the top of a spike, and measure the 1-minute noise the stop has to survive.

WHY: ARVIND, 28 Oct: bought 518.15 at 12:26, stopped at 511.85 at 12:27 (1.2 % stop, one minute), then ran to ~530. The stock was up ~6 % on the day and the
buy was a MARKET order sent right after a vertical move: the fill was at the top of the thrust and the ordinary give-back of a thrust (1 % or so, inside a
minute) was bigger than the stop. Entries at the top of a spike lose the first minute far more often than any other entry.

TWO LIVE READS, both from the in-memory tick buffer / exchange day stats (no network, no DB):
  1. spike_reason():       "SPIKE_CHASE:..." when the stock is
       (a) a day spike  - up >= SPIKE_DAY_PCT vs the previous close AND in the top of today's range (range position >= SPIKE_RANGE_POS), or
       (b) mid-thrust   - up >= SPIKE_THRUST_PCT inside the last SPIKE_THRUST_WINDOW_S seconds AND within SPIKE_NEAR_HIGH_PCT of that window's high.
     The reason starts with SPIKE_CHASE, which orders/pullback_rearm.py arms on: the symbol is looked at again after a real pullback and bounce.
  2. avg_1m_range_pct():   mean (high-low)/price % of the last few 1-minute buckets. orders/adaptive.py::apply_stop_floor keeps the stop at least
     ADAPTIVE_STOP_M1_MULT x this, so the stop is not smaller than what the stock does in two ordinary minutes.

Every read fails OPEN (None = no opinion) on missing / thin data or any error. SPIKE_GUARD_MODE: live (refuse) | log (log only) | off.
Pure helpers take plain (timestamp, price) lists so they are testable without a feed. Never raises into the trading loop.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional, Sequence, Tuple

import config
from orders import guard_stats

logger = logging.getLogger("position-stocks-entry-timing")

Tick = Tuple[float, float]


def _cfg(name: str, default):
    try:
        v = getattr(config, name)
        return default if v is None else v
    except Exception:  # noqa: BLE001
        return default


def mode() -> str:
    m = str(_cfg("SPIKE_GUARD_MODE", "live") or "live").strip().lower()
    return m if m in ("live", "log", "off") else "live"


def _clean(ticks: Optional[Sequence]) -> List[Tick]:
    out: List[Tick] = []
    for t in ticks or ():
        try:
            ts, px = float(t[0]), float(t[1])
        except (TypeError, ValueError, IndexError):
            continue
        if ts > 0 and px > 0:
            out.append((ts, px))
    return out


def window_stats(ticks: Optional[Sequence], now: float, window_s: float, min_ticks: int = 3) -> Optional[dict]:
    """Stats of the last `window_s` seconds of ticks: {n, low, high, last, thrust_pct, off_high_pct}.
    thrust_pct = (last - window low) / window low, but only counted when the low came BEFORE the high (a rise); off_high_pct = (high - last) / high.
    None when there are fewer than `min_ticks` ticks in the window (no opinion). Pure."""
    data = [t for t in _clean(ticks) if now - window_s <= t[0] <= now + 1.0]
    if len(data) < max(1, int(min_ticks)):
        return None
    prices = [p for _t, p in data]
    low, high, last = min(prices), max(prices), prices[-1]
    i_low = prices.index(low)
    i_high = len(prices) - 1 - prices[::-1].index(high)
    thrust = (last - low) / low * 100.0 if i_low <= i_high else 0.0
    return {"n": len(data), "low": low, "high": high, "last": last,
            "thrust_pct": thrust, "off_high_pct": (high - last) / high * 100.0}


def avg_1m_range_pct(ticks: Optional[Sequence], now: float, minutes: int = 5, min_buckets: int = 3, min_ticks_per_bucket: int = 3) -> Optional[float]:
    """Mean (high-low)/mid % over the 1-minute buckets of the last `minutes` minutes (the bucket still forming counts too). A bucket with fewer than
    `min_ticks_per_bucket` ticks is skipped (a thin minute understates the range); fewer than `min_buckets` usable buckets = None. Pure."""
    data = [t for t in _clean(ticks) if now - minutes * 60.0 <= t[0] <= now + 1.0]
    if not data:
        return None
    buckets: dict = {}
    for ts, px in data:
        buckets.setdefault(int(ts // 60), []).append(px)
    ranges: List[float] = []
    for _k, pxs in buckets.items():
        if len(pxs) < min_ticks_per_bucket:
            continue
        hi, lo = max(pxs), min(pxs)
        mid = (hi + lo) / 2.0
        if mid > 0:
            ranges.append((hi - lo) / mid * 100.0)
    if len(ranges) < min_buckets:
        return None
    avg = sum(ranges) / len(ranges)
    return avg if avg > 0 else None


def day_limit_pct(atr_pct: Optional[float]) -> float:
    """Day-gain that counts as a spike. Adaptive: SPIKE_DAY_ATR_MULT x the stock's own daily ATR %, clamped to SPIKE_DAY_MIN_PCT..SPIKE_DAY_MAX_PCT
    (a +4 % day is routine for a 5 % ATR name and a spike for a 1.5 % one). Unknown ATR = the fixed SPIKE_DAY_PCT. 0 = test off. Pure."""
    fixed = float(_cfg("SPIKE_DAY_PCT", 4.0))
    if fixed <= 0:
        return 0.0
    mult = float(_cfg("SPIKE_DAY_ATR_MULT", 0.0))
    if atr_pct and atr_pct > 0 and mult > 0:
        lo, hi = float(_cfg("SPIKE_DAY_MIN_PCT", 2.5)), float(_cfg("SPIKE_DAY_MAX_PCT", 6.0))
        return max(lo, min(hi, mult * float(atr_pct)))
    return fixed


def thrust_limit_pct(m1_range_pct: Optional[float]) -> float:
    """Rise inside the window that counts as a thrust. Adaptive: SPIKE_THRUST_M1_MULT x the stock's average 1-minute range, clamped to
    SPIKE_THRUST_MIN_PCT..SPIKE_THRUST_MAX_PCT. Unknown = the fixed SPIKE_THRUST_PCT. 0 = test off. Pure."""
    fixed = float(_cfg("SPIKE_THRUST_PCT", 1.5))
    if fixed <= 0:
        return 0.0
    mult = float(_cfg("SPIKE_THRUST_M1_MULT", 0.0))
    if m1_range_pct and m1_range_pct > 0 and mult > 0:
        lo, hi = float(_cfg("SPIKE_THRUST_MIN_PCT", 1.0)), float(_cfg("SPIKE_THRUST_MAX_PCT", 2.5))
        return max(lo, min(hi, mult * float(m1_range_pct)))
    return fixed


def judge(day_pct: Optional[float], range_pos: Optional[float], stats: Optional[dict],
          atr_pct: Optional[float] = None, m1_range_pct: Optional[float] = None) -> Optional[str]:
    """The decision on plain numbers. Reason string or None. The limits adapt to the stock's own daily ATR % / 1-minute range when given. Pure."""
    day_lim, pos_lim = day_limit_pct(atr_pct), float(_cfg("SPIKE_RANGE_POS", 0.85))
    if day_lim > 0 and day_pct is not None and range_pos is not None and day_pct >= day_lim and range_pos >= pos_lim:
        return (f"SPIKE_CHASE:DAY_SPIKE:up {day_pct:.1f}% on the day (limit {day_lim:.1f}%) and at range position {range_pos:.2f} "
                f"(limit {pos_lim:.2f}) - wait for a pullback")
    th_lim, near_lim = thrust_limit_pct(m1_range_pct), float(_cfg("SPIKE_NEAR_HIGH_PCT", 0.25))
    if th_lim > 0 and stats is not None and stats["thrust_pct"] >= th_lim and stats["off_high_pct"] <= near_lim:
        win = int(float(_cfg("SPIKE_THRUST_WINDOW_S", 180.0)))
        return (f"SPIKE_CHASE:THRUST:+{stats['thrust_pct']:.2f}% inside {win}s (limit {th_lim:.2f}%) and only {stats['off_high_pct']:.2f}% under "
                f"the {win}s high (limit {near_lim:.2f}%) - wait for it to settle")
    return None


def _day_numbers(symbol: str, ltp: float) -> Tuple[Optional[float], Optional[float]]:
    """(day % vs previous close, range position 0..1) from the exchange day stats; either may be None."""
    from feed import ws_client
    day_pct = None
    range_pos = None
    _ds_fn = getattr(ws_client, "get_day_stats", None)
    ds = _ds_fn(symbol) if _ds_fn else None
    prev_close = ds[3] if ds and len(ds) > 3 else None
    if prev_close and prev_close > 0 and ltp > 0:
        day_pct = (ltp - prev_close) / prev_close * 100.0
    _rng_fn = getattr(ws_client, "get_day_range", None)
    rng = _rng_fn(symbol) if _rng_fn else None
    if rng:
        lo, hi = rng
        if hi > lo and ltp > 0:
            range_pos = max(0.0, min(1.0, (ltp - lo) / (hi - lo)))
    return day_pct, range_pos


def spike_reason(symbol: str, ltp: float, now: Optional[float] = None) -> Optional[str]:
    """SPIKE_CHASE reason for the entry path, else None. live -> the reason; log -> logged, None returned; off -> None. Never raises."""
    try:
        m = mode()
        if m == "off" or ltp <= 0:
            return None
        from feed import ws_client
        t = time.time() if now is None else now
        day_pct, range_pos = _day_numbers(symbol, ltp)
        ticks = ws_client.get_tick_buffer(symbol)
        stats = window_stats(ticks, t, float(_cfg("SPIKE_THRUST_WINDOW_S", 180.0)), min_ticks=int(_cfg("SPIKE_MIN_TICKS", 8)))
        atr_pct = None
        try:
            from screening import prev_day
            pdv = prev_day.peek(symbol)
            atr_pct = getattr(pdv, "atr_pct", None) if pdv else None
        except Exception as e:  # noqa: BLE001 - no daily ATR = the fixed day limit
            logger.debug("entry-timing: daily ATR for %s unavailable: %s", symbol, e)
        why = judge(day_pct, range_pos, stats, atr_pct=atr_pct, m1_range_pct=avg_1m_range_pct(ticks, t))
        if why and m == "log":
            logger.info("entry-timing (log only): %s would be skipped - %s", symbol, why)
            guard_stats.bump("spike", "log_only")
            return None
        # E257: no_data = no day % / range position, or a cold daily-ATR cache (the fixed day limit was used, not the stock's own volatility)
        guard_stats.bump("spike", "held" if why else "no_data" if (day_pct is None or range_pos is None or atr_pct is None) else "passed")
        return why
    except Exception as e:  # noqa: BLE001
        logger.debug("entry-timing: spike check failed for %s (no opinion): %s", symbol, e)
        guard_stats.bump("spike", "error")
        return None


def live_avg_1m_range_pct(symbol: str, now: Optional[float] = None) -> Optional[float]:
    """avg_1m_range_pct() on the live tick buffer. None on any problem."""
    try:
        from feed import ws_client
        return avg_1m_range_pct(ws_client.get_tick_buffer(symbol), time.time() if now is None else now)
    except Exception as e:  # noqa: BLE001
        logger.debug("entry-timing: 1m range failed for %s (no opinion): %s", symbol, e)
        return None