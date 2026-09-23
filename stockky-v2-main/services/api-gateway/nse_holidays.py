"""
NSE trading holidays (closed days). Keep updated annually.

Sources: NSE circulars / typical all-India holidays. Not a substitute for the
official calendar — when in doubt, treat as open and let market data fail soft.
"""
from __future__ import annotations

from datetime import date
from typing import Set

# Fixed / common market holidays (YYYY-MM-DD). Extend each year.
_NSE_HOLIDAYS: Set[date] = {
    # 2025
    date(2025, 2, 26),   # Mahashivratri
    date(2025, 3, 14),   # Holi
    date(2025, 3, 31),   # Id-Ul-Fitr (approx)
    date(2025, 4, 10),   # Mahavir Jayanti
    date(2025, 4, 14),   # Dr Ambedkar Jayanti / Good Friday window
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 1),    # Maharashtra Day
    date(2025, 8, 15),   # Independence Day
    date(2025, 8, 27),   # Ganesh Chaturthi
    date(2025, 10, 2),   # Gandhi Jayanti
    date(2025, 10, 21),  # Diwali Laxmi Pujan (typical)
    date(2025, 10, 22),  # Balipratipada (typical)
    date(2025, 11, 5),   # Gurunanak Jayanti
    date(2025, 12, 25),  # Christmas
    # 2026 — AUDIT FIX (2026-09-14, live incident): this set was missing
    # 2026-09-14 (Ganesh Chaturthi) entirely, and several of the other
    # entries below were wrong/placeholder ("approx") guesses rather than
    # the actual gazetted dates. Combined with position-stocks-service's
    # and real-trade-service's own is_market_open_ist() having ZERO holiday
    # awareness at all, today's cycles ran and notifications fired on a
    # day the exchange was actually closed. Replaced with the verified
    # official 2026 NSE trading-holiday list (cross-checked against the
    # Zerodha holiday calendar, which mirrors NSE's circular) as of
    # 2026-09-14. This list is duplicated in three other places with no
    # shared import path between services — see tz_utils.py's
    # is_market_open_ist() in position-stocks-service/real-trade-service
    # and notification-scheduler-service/scheduler/run_once.py's
    # HOLIDAYS_2026 for the others; run scripts/check_holiday_lists_sync.py
    # after editing any one of them.
    date(2026, 1, 15),   # Maharashtra Municipal Corporation elections
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 3),    # Holi
    date(2026, 3, 26),   # Ram Navami
    date(2026, 3, 31),   # Mahavir Jayanti
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 28),   # Bakri Eid (Eid ul-Adha)
    date(2026, 6, 26),   # Muharram
    date(2026, 9, 14),   # Ganesh Chaturthi — the date missing that caused this fix
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra
    date(2026, 11, 10),  # Diwali Balipratipada
    date(2026, 11, 24),  # Guru Nanak Jayanti
    date(2026, 12, 25),  # Christmas
}


def is_nse_holiday(d: date) -> bool:
    return d in _NSE_HOLIDAYS


def holiday_name(d: date) -> str | None:
    if d not in _NSE_HOLIDAYS:
        return None
    return "NSE holiday"
