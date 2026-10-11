"""group321: position-stocks - the loop that reconciles exits and runs the EOD square-off uses the calendar and clock only;
the price-based "closed" verdict only stops NEW entries."""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import tz_utils

IST = ZoneInfo("Asia/Kolkata")


class _FakeDT(datetime):
    @classmethod
    def now(cls, tz=None):
        base = datetime(2026, 10, 12, 10, 30, tzinfo=IST)
        return base.astimezone(tz) if tz else base


def test_closed_verdict_stops_entries_but_not_exit_handling(monkeypatch):
    import session_proof_client
    monkeypatch.setattr(session_proof_client, "closed_for_day", lambda day, refresh=True: True)
    monkeypatch.setattr(tz_utils, "datetime", _FakeDT)
    assert tz_utils.is_market_open_ist() is False
    assert tz_utils.is_market_open_ist(honor_session_proof=False) is True


def test_calendar_and_clock_still_apply_without_the_verdict():
    assert tz_utils.is_market_open_ist(datetime(2026, 10, 10, 10, 30, tzinfo=IST), honor_session_proof=False) is False   # Saturday
    assert tz_utils.is_market_open_ist(datetime(2026, 10, 12, 16, 0, tzinfo=IST), honor_session_proof=False) is False    # after close


def test_main_loops_use_calendar_only_and_entries_recheck():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "main.py"), encoding="utf-8").read()
    assert len(re.findall(r"if not is_market_open_ist\(honor_session_proof=False\):", src)) >= 2
    assert '_closed_for_day(ist_today_str())' in src and 'if _proof_closed:' in src
    assert "MARKET_CLOSED_PRICE_PROOF" in src
