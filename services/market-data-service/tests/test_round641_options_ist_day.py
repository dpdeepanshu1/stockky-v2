"""Round 641 (E667): options.list_expiries used date.today(), which is the UTC date on the servers, so from 00:00 to 05:30 IST it was the previous IST day.
It now uses the IST date (`_ist_today`). Plain functions; `python3 <this file>` runs them without pytest. No network."""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dhan_data import options as M  # noqa: E402


def test_ist_today_is_the_ist_calendar_day():
    assert M._ist_today(datetime(2026, 11, 18, 20, 0, tzinfo=timezone.utc)) == date(2026, 11, 19)    # 01:30 IST on the 19th
    assert M._ist_today(datetime(2026, 11, 18, 18, 29, tzinfo=timezone.utc)) == date(2026, 11, 18)   # 23:59 IST on the 18th
    assert M._ist_today(datetime(2026, 11, 18, 18, 30, tzinfo=timezone.utc)) == date(2026, 11, 19)   # 00:00 IST on the 19th
    assert M._ist_today(datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc)) == date(2026, 11, 19)


def test_list_expiries_default_day_is_the_ist_day():
    with M._lock:
        saved = dict(M._contracts)
        M._contracts.clear()
        M._contracts[("NIFTY", "2026-11-17")] = object()
        M._contracts[("NIFTY", "2026-11-19")] = object()
    real = M._ist_today
    try:
        M._ist_today = lambda now_utc=None: date(2026, 11, 19)       # 01:30 IST on the 19th: the 17th is gone, the 19th (today) stays
        assert M.list_expiries("NIFTY") == ["2026-11-19"]
        M._ist_today = lambda now_utc=None: date(2026, 11, 20)
        assert M.list_expiries("NIFTY") == []
        assert M.list_expiries("NIFTY", today=date(2026, 11, 7)) == ["2026-11-17", "2026-11-19"]
    finally:
        M._ist_today = real
        with M._lock:
            M._contracts.clear()
            M._contracts.update(saved)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(e))
    sys.exit(1 if fails else 0)
