"""Round 641 (E667): structure_obs.evaluate_pending and structure_obs.report used date.today(), which is the UTC date on the servers, so from 00:00 to 05:30 IST it was the previous IST day.
It now uses the IST date (`_ist_today`). Plain functions; `python3 <this file>` runs them without pytest. No network."""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import structure_obs as M  # noqa: E402


def test_ist_today_is_the_ist_calendar_day():
    assert M._ist_today(datetime(2026, 11, 18, 20, 0, tzinfo=timezone.utc)) == date(2026, 11, 19)    # 01:30 IST on the 19th
    assert M._ist_today(datetime(2026, 11, 18, 18, 29, tzinfo=timezone.utc)) == date(2026, 11, 18)   # 23:59 IST on the 18th
    assert M._ist_today(datetime(2026, 11, 18, 18, 30, tzinfo=timezone.utc)) == date(2026, 11, 19)   # 00:00 IST on the 19th
    assert M._ist_today(datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc)) == date(2026, 11, 19)


def test_report_window_starts_from_the_ist_day_when_no_date_is_given():
    class _Q:
        def order_by(self, *a):
            return self

        def limit(self, *a):
            return self

        def all(self):
            return []

    class _DB:
        def query(self, M_):
            return _Q()

    class _M:
        class id:  # noqa: N801
            @staticmethod
            def desc():
                return None

    real = M._ist_today
    try:
        M._ist_today = lambda now_utc=None: date(2026, 11, 19)
        out = M.report(_DB(), _M, days=10)
        assert out["window_days"] == 10
    finally:
        M._ist_today = real


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
