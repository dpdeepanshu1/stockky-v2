"""orders/live_anchor.py - anchor the stop / target legs to the live price at order time (2026-10-22).

Why: the automatic entry builds its stop and target from the SCAN price (candidate.current_ltp), then sends a MARKET order seconds later. When the
price moved in between, the legs sit at the wrong distance from the real fill. IONEXCHANG 22 Oct: signal 354.60, filled 352.30 (-0.65 %), stop
351.75 = signal x (1 - 0.8 %), so the stop was 0.15 % under the fill and the trade stopped out 21 s after entry (planned stop 0.8 %). The same gap the
other way (EMAMILTD, filled 0.43 % above the signal) leaves a wide stop and a short target. reconcile re-arms the legs from the fill, but only on a
later pass, and the price guard only rejects a rise above the signal, never a fall.

What this does: reads the freshest buffered tick (same source and age limit as the price guard) and moves BOTH legs to that price, keeping the planned
stop % / target % (so reward:risk and the RR floor are unchanged). A price move below the signal is therefore no longer a hidden tight stop.
Pure helpers; every path fails open (no tick / bad data -> the levels are returned unchanged). ENTRY_ANCHOR_TO_LIVE=0 switches it off.
"""
from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any, Optional, Tuple

logger = logging.getLogger("position-stocks-live-anchor")

_MIN_DRIFT_PCT = 0.02   # below this the legs are already where the fill will be, to within a tick or two


def live_price(symbol: str, max_age_s: Optional[float] = None) -> Optional[float]:
    """Last buffered tick of `symbol` when it is fresher than max_age_s (config.ENTRY_MAX_TICK_AGE_S, 0 = no age limit); else None."""
    try:
        import config
        from feed import ws_client
        if max_age_s is None:
            max_age_s = float(getattr(config, "ENTRY_MAX_TICK_AGE_S", 45.0) or 0.0)
        buf = ws_client.get_tick_buffer(symbol)
        if not buf:
            return None
        ts, px = buf[-1]
        if not px or float(px) <= 0:
            return None
        if max_age_s > 0 and time.time() - float(ts) > max_age_s:
            return None
        return float(px)
    except Exception as e:  # noqa: BLE001 - fail open
        logger.debug("live_anchor: no live price for %s: %s", symbol, e)
        return None


def reanchor(levels: Any, signal_price: float, live: Optional[float], enabled: Optional[bool] = None,
             round_fn: Any = None) -> Tuple[Any, Optional[float]]:
    """(levels, drift_pct). levels moved to `live` with the same stop % / target %; drift_pct = (live - signal) / signal x 100 when moved, else None.
    Unchanged when disabled, no live price, a drift under _MIN_DRIFT_PCT, or the moved levels would not bracket the live price."""
    try:
        if enabled is None:
            import config
            enabled = bool(getattr(config, "ENTRY_ANCHOR_TO_LIVE", True))
        if not enabled or live is None or not (live > 0) or not (signal_price and signal_price > 0):
            return levels, None
        drift = (live - signal_price) / signal_price * 100.0
        if abs(drift) < _MIN_DRIFT_PCT:
            return levels, None
        sp, tp = float(levels.stop_pct), float(levels.target_pct)
        if not (sp > 0 and tp > 0):
            return levels, None
        if round_fn is None:
            from execution.dhan_client import round_to_tick as round_fn
        stop = round_fn(round(live * (1 - sp / 100.0), 2))
        target = round_fn(round(live * (1 + tp / 100.0), 2))
        if not (0 < stop < live < target):
            return levels, None
        return dataclasses.replace(levels, stop_price=stop, target_price=target), drift
    except Exception as e:  # noqa: BLE001 - fail open
        logger.debug("live_anchor: re-anchor skipped: %s", e)
        return levels, None
