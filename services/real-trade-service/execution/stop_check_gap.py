"""E285 round 158 (plan item 16): late stop-check detection. Kept in its own module (no database / broker imports) so it is unit-testable."""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

def warn_s() -> float:
    try:
        raw = (os.getenv("EXIT_CHECK_GAP_WARN_S") or "").strip()
        v = float(raw) if raw else 45.0
        return v if v >= 0 else 45.0
    except (TypeError, ValueError):
        return 45.0


_state: dict = {}            # mode -> (monotonic time of the last COMPLETED stop check, positions it evaluated)
_last_warn: dict = {}


def note_done(mode: str, evaluated, now_m: Optional[float] = None) -> Optional[float]:
    """E285 round 158 (plan item 16): the stop check of a same-day position is software (price vs stop each tick), so it is only as fresh as the last COMPLETED
    check. Called after every exit evaluation (the fast tick and the full cycle's exit stage). When the gap since the previous completed check exceeds
    EXIT_CHECK_GAP_WARN_S (default 45 s; ticks run every 5-10 s; 0 = off) while that previous check had open positions, ONE WARNING is logged (at most every
    120 s per mode) and the gap in seconds is returned so the caller can send a Telegram alert; otherwise None. Observation only. Never raises."""
    try:
        import time as _t
        now_m = _t.monotonic() if now_m is None else float(now_m)
        prev = _state.get(mode)
        _state[mode] = (now_m, int(evaluated or 0))
        limit = warn_s()
        if limit <= 0 or prev is None or prev[1] <= 0:
            return None
        gap = now_m - prev[0]
        if gap <= limit:
            return None
        if now_m - _last_warn.get(mode, -1e12) < 120.0:
            return None
        _last_warn[mode] = now_m
        logger.warning("stop check %s ran LATE: %.0fs since the previous completed check (> %.0fs) with %d open position(s) - their software stops were not looked at "
                       "in between (EXIT_CHECK_GAP_WARN_S)", mode, gap, limit, prev[1])
        return gap
    except Exception:  # noqa: BLE001
        return None




def reset(mode: str) -> None:
    """Forget the last check (the market closed): no gap is counted across the close."""
    _state.pop(mode, None)
