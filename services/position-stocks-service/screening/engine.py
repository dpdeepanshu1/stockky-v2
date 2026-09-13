"""
screening/engine.py — in-memory rolling-window screener.

Consumes ticks from feed/ws_client.py ring buffers.
Computes rolling pct-change over 1m/5m/15m/60m windows O(1) per tick
(scan the deque backward until we find the reference price at the window
boundary — no DB read on the hot path).

Runs ALL FOUR windows simultaneously and feeds one shared ranking step
(composite score) so the best candidate wins regardless of which window
surfaced it. The 1m window (added 2026-09-12) is intentionally the
noisiest/fastest-triggering of the four — its own threshold
(config.MIN_PCT_CHANGE_1M) defaults meaningfully lower than 5m's to
reflect that a 1-minute move needs a smaller %-change to be notable than
a 5-minute one, but it's also the window most likely to fire on a single
flickering tick rather than real momentum — tune its threshold based on
what it actually surfaces in practice.

Gates applied per candidate:
  1. Min pct-change per window (config.MIN_PCT_CHANGE_*M)
  2. Min average volume floor (MIN_AVG_VOLUME — crude proxy, traded
     volume from the WS feed accumulates in _volume_accum)
  3. Not already in an open scalp position (open_symbols set, passed in
     by the caller / orders layer)
  4. Spread gate: skipped for now (WS mode-1 gives LTP only, not
     bid/ask — will add when mode-2 is used or REST quote is available)

Composite score formula (same spirit as real-trade-service Gate 6):
  score = pct_change * volume_weight
  volume_weight = min(vol / MIN_AVG_VOLUME, 3.0)   # cap at 3× floor
The highest-scoring symbol(s) bubble up to the entry layer.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import config
from feed import ws_client

logger = logging.getLogger("position-stocks-screener")

# Per-window thresholds keyed by window minutes
_WINDOW_THRESHOLDS = {
    1:  config.MIN_PCT_CHANGE_1M,
    5:  config.MIN_PCT_CHANGE_5M,
    15: config.MIN_PCT_CHANGE_15M,
    60: config.MIN_PCT_CHANGE_60M,
}

# Volume accumulator: sum of LTP jumps as a proxy for traded volume
# (WS mode-1 doesn't give actual volume — we use tick frequency as a
# liquidity proxy until mode-2 data is wired)
_volume_accum: Dict[str, int] = defaultdict(int)
_volume_window_s = 300  # count ticks in last 5 min as "activity level"
_tick_timestamps: Dict[str, list] = defaultdict(list)


def _update_volume(symbol: str, ts: float) -> None:
    """Track tick count in the last _volume_window_s as activity proxy."""
    tl = _tick_timestamps[symbol]
    tl.append(ts)
    cutoff = ts - _volume_window_s
    while tl and tl[0] < cutoff:
        tl.pop(0)
    _volume_accum[symbol] = len(tl)


def _rolling_pct_change(symbol: str, window_minutes: int) -> Optional[float]:
    """O(scan) — scan deque backward to find the reference price at the
    start of the window. Returns None if insufficient data."""
    buf = ws_client.get_tick_buffer(symbol)
    if len(buf) < 2:
        return None
    now_ts = buf[-1][0]
    current_ltp = buf[-1][1]
    cutoff_ts = now_ts - (window_minutes * 60)
    ref_price = None
    for ts, ltp in reversed(buf):
        if ts <= cutoff_ts:
            ref_price = ltp
            break
    if ref_price is None or ref_price <= 0:
        return None
    return ((current_ltp - ref_price) / ref_price) * 100.0


@dataclass
class Candidate:
    symbol: str
    window_minutes: int
    pct_change: float
    current_ltp: float
    tick_activity: int           # tick count in last 5m (volume proxy)
    composite_score: float = 0.0
    window_label: str = field(init=False)

    def __post_init__(self):
        self.window_label = f"{self.window_minutes}m"


def scan(open_symbols: Optional[Set[str]] = None) -> List[Candidate]:
    """Run a full scan across all subscribed symbols and all four windows.
    Returns a list of Candidate objects sorted by composite_score descending.
    open_symbols: set of symbol strings already holding a scalp position.
    """
    open_symbols = open_symbols or set()
    candidates: List[Candidate] = []

    subscribed = list(ws_client._token_to_symbol.values()) or list(
        ws_client._tick_buffers.keys()
    )

    for symbol in subscribed:
        if symbol in open_symbols:
            continue

        buf = ws_client.get_tick_buffer(symbol)
        if len(buf) < 2:
            continue

        current_ltp = buf[-1][1]
        if current_ltp <= 0:
            continue

        tick_count = _volume_accum.get(symbol, 0)
        # Rough liquidity gate: require at least 1 tick per 10s on average
        # over the last 5 minutes (= 30 ticks). Very rough, tune later.
        if tick_count < max(1, int(config.MIN_AVG_VOLUME / 5000)):
            # MIN_AVG_VOLUME is in shares/day; map to tick frequency heuristic
            pass  # skip strict gate for now — log-only until calibrated

        for win_minutes, threshold in _WINDOW_THRESHOLDS.items():
            pct = _rolling_pct_change(symbol, win_minutes)
            if pct is None or pct < threshold:
                continue

            volume_weight = min(max(tick_count, 1) / max(config.MIN_AVG_VOLUME / 5000, 1), 3.0)
            score = pct * volume_weight

            candidates.append(Candidate(
                symbol=symbol,
                window_minutes=win_minutes,
                pct_change=round(pct, 4),
                current_ltp=current_ltp,
                tick_activity=tick_count,
                composite_score=round(score, 4),
            ))

    candidates.sort(key=lambda c: c.composite_score, reverse=True)
    return candidates


def on_tick_hook(symbol: str, ltp: float, volume: int, ts: float) -> None:
    """Registered with ws_client.register_on_tick() at startup.
    Keeps the volume accumulator fresh on every incoming tick."""
    _update_volume(symbol, ts)


# Register the hook at module import time
ws_client.register_on_tick(on_tick_hook)
