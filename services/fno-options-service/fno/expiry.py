"""fno/expiry.py - weekly expiry dates for the BACKTESTS (the live engine reads the real expiry list from Dhan and never uses this).

Facts as of Nov 2026 (parameters, not truths: the exchanges change them): NIFTY weekly expires on TUESDAY (NSE), SENSEX weekly on THURSDAY (BSE). When the expiry weekday is a
holiday the expiry moves to the previous trading day. A backtest over a period when the weekday was different (Nifty was Thursday before Sep 2025; Sensex was Friday before
Sep 2025) must pass `weekday=` for that period - the SIM / EOD runners take the weekday as an option for exactly this reason."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, Sequence

WEEKLY_WEEKDAY: Dict[str, int] = {"NIFTY": 1, "SENSEX": 3}      # Monday = 0


def _is_trading_day(d: date, holidays: Sequence[str]) -> bool:
    return d.weekday() < 5 and d.isoformat() not in set(holidays)


def next_weekly_expiry(underlying: str, on_or_after: date, holidays: Sequence[str] = (), weekday: int | None = None) -> date:
    """The first weekly expiry date that is >= `on_or_after`, holiday-adjusted (the adjusted date may fall before `on_or_after`: then the next week's is returned)."""
    wd = WEEKLY_WEEKDAY[underlying.upper()] if weekday is None else weekday
    d = on_or_after
    for _ in range(30):
        cand = d + timedelta(days=(wd - d.weekday()) % 7)
        adj = cand
        while not _is_trading_day(adj, holidays):
            adj -= timedelta(days=1)
        if adj >= on_or_after:
            return adj
        d = cand + timedelta(days=1)
    raise ValueError("no expiry found")


def expiry_for_entry(underlying: str, today: date, min_days: int = 1, block_expiry_day: bool = True, holidays: Sequence[str] = (), weekday: int | None = None) -> date:
    need = max(min_days, 1 if block_expiry_day else 0)
    return next_weekly_expiry(underlying, today + timedelta(days=need), holidays, weekday)
