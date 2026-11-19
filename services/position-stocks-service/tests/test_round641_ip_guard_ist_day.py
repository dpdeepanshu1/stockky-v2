"""Round 641 (E667): ip_guard.summarize_ip_status used date.today(), which is the UTC date on the servers, so from 00:00 to 05:30 IST it was the previous IST day.
It now uses the IST date (`_ist_today`). Plain functions; `python3 <this file>` runs them without pytest. No network."""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import types


def _stub(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


try:
    import sqlalchemy.orm  # noqa: F401
except Exception:  # noqa: BLE001
    _stub("sqlalchemy")
    _stub("sqlalchemy.orm", Session=object)
try:
    import httpx  # noqa: F401
except Exception:  # noqa: BLE001
    _stub("httpx", get=lambda *a, **k: None)
try:
    from auth import dhan_credentials_ro  # noqa: F401
except Exception:  # noqa: BLE001
    _stub("auth.dhan_credentials_ro", get_decrypted_credentials=lambda db: ("C1", "T1"))

from orders import ip_guard as M  # noqa: E402


def test_ist_today_is_the_ist_calendar_day():
    assert M._ist_today(datetime(2026, 11, 18, 20, 0, tzinfo=timezone.utc)) == date(2026, 11, 19)    # 01:30 IST on the 19th
    assert M._ist_today(datetime(2026, 11, 18, 18, 29, tzinfo=timezone.utc)) == date(2026, 11, 18)   # 23:59 IST on the 18th
    assert M._ist_today(datetime(2026, 11, 18, 18, 30, tzinfo=timezone.utc)) == date(2026, 11, 19)   # 00:00 IST on the 19th
    assert M._ist_today(datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc)) == date(2026, 11, 19)


def test_change_allowed_uses_the_ist_day_when_no_date_is_given():
    data = {"ipMatchStatus": "PRIMARY_MATCHED", "primaryIP": "1.2.3.4", "detectedIP": "1.2.3.4", "ordersAllowed": True,
            "modifyDatePrimary": "2026-11-19"}
    real = M._ist_today
    try:
        M._ist_today = lambda now_utc=None: date(2026, 11, 19)       # 01:30 IST on the 19th (the UTC date would still be the 18th)
        assert M.summarize_ip_status(data)["primary_change_allowed"] is True
        M._ist_today = lambda now_utc=None: date(2026, 11, 18)
        assert M.summarize_ip_status(data)["primary_change_allowed"] is False
    finally:
        M._ist_today = real


def test_an_explicit_today_still_wins():
    data = {"ipMatchStatus": "PRIMARY_MATCHED", "modifyDatePrimary": "2026-11-19"}
    assert M.summarize_ip_status(data, today=date(2026, 11, 20))["primary_change_allowed"] is True
    assert M.summarize_ip_status(data, today=date(2026, 11, 18))["primary_change_allowed"] is False


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
