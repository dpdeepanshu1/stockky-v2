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
    # 2026
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 3),    # Holi (approx)
    date(2026, 3, 26),   # Holi / related
    date(2026, 3, 31),   # Ram Navami (approx)
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 8, 15),   # Independence Day
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra (approx)
    date(2026, 11, 8),   # Diwali (approx — verify each year)
    date(2026, 11, 9),   # Diwali related
    date(2026, 11, 24),  # Gurunanak Jayanti (approx)
    date(2026, 12, 25),  # Christmas
}


def is_nse_holiday(d: date) -> bool:
    return d in _NSE_HOLIDAYS


def holiday_name(d: date) -> str | None:
    if d not in _NSE_HOLIDAYS:
        return None
    return "NSE holiday"
