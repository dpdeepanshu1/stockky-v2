"""orders/manual_buy_window.py - E220: when a manual BUY is allowed.

The automatic path only runs inside the trading loop (market hours, before the EOD sweep). The manual route (POST /positions/manual/buy ->
attempt_manual_entry) checked neither: a click after the market closed went to Dhan as an order, and a click after today's one-shot EOD square-off
(`eod_squareoff_fired_date == today`, the sweep never runs twice a day) opened an INTRADAY position that nothing would flatten before Dhan's own cut-off.

check() returns a refusal message or None. Never raises: any error fails OPEN (the same rule as every other gate in orders/entry.py), so a broken
calendar can never block the admin. MANUAL_BUY_WINDOW_GUARD=false restores the old behaviour.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import config

logger = logging.getLogger("position-stocks-manual-window")


def check(gate: Any, today: str, now: Optional[datetime] = None) -> Optional[str]:
    try:
        if not getattr(config, "MANUAL_BUY_WINDOW_GUARD", True):
            return None
        import tz_utils
        if not tz_utils.is_market_open_ist(now):
            return "The market is closed - a manual BUY can only be placed between 09:15 and 15:30 IST on a trading day."
        fired = getattr(gate, "eod_squareoff_fired_date", None) if gate is not None else None
        if fired == today:
            return ("Today's end-of-day square-off has already run - a position opened now would not be closed by it. "
                    "Try again on the next trading day.")
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("manual buy window check failed (fail open): %s", e)
        return None
