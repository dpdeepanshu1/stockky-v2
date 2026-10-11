"""group321: the price-based "closed" verdict gates NEW entries only. Stops, emergency exits and the EOD square-off use the
calendar and clock alone, so a wrong verdict (stale feed) can never leave an open position without its stop."""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import tz_utils

IST = ZoneInfo("Asia/Kolkata")


class _FakeDT(datetime):
    @classmethod
    def now(cls, tz=None):
        base = datetime(2026, 10, 12, 10, 30, tzinfo=IST)      # an ordinary Monday, inside market hours
        return base.astimezone(tz) if tz else base


def _closed_verdict(monkeypatch):
    import session_proof_client
    monkeypatch.setattr(session_proof_client, "closed_for_day", lambda day, refresh=True: True)
    monkeypatch.setattr(tz_utils, "datetime", _FakeDT)


def test_closed_verdict_stops_entries_but_not_exits(monkeypatch):
    _closed_verdict(monkeypatch)
    assert tz_utils.is_market_open_ist() is False                              # entries: verdict honoured
    assert tz_utils.is_market_open_ist(honor_session_proof=False) is True      # exits: calendar + clock only


def test_exit_check_still_respects_calendar_and_clock(monkeypatch):
    _closed_verdict(monkeypatch)
    sat = datetime(2026, 10, 10, 10, 30, tzinfo=IST)
    night = datetime(2026, 10, 12, 20, 0, tzinfo=IST)
    assert tz_utils.is_market_open_ist(sat, honor_session_proof=False) is False
    assert tz_utils.is_market_open_ist(night, honor_session_proof=False) is False


def test_auto_pilot_exit_paths_do_not_use_the_price_verdict():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "execution", "auto_pilot.py"), encoding="utf-8").read()
    assert re.search(r"if not is_market_open_ist\(honor_session_proof=False\):\s*\n\s*return\s*\n\s*\n?\s*# .*Protective exits", src)
    assert re.search(r'eod_squareoff_last_run", None\) != today\s*\n\s*and is_market_open_ist\(honor_session_proof=False\)', src)


def test_no_price_streak_alerts_once_per_cooldown(monkeypatch):
    from exit_engine import exit as ex
    sent = []
    monkeypatch.setattr(ex, "notify_sync", lambda text: sent.append(text))
    monkeypatch.setenv("EXIT_NO_PRICE_ALERT_CYCLES", "3")
    monkeypatch.setenv("EXIT_NO_PRICE_ALERT_COOLDOWN_S", "900")
    ex._NO_PRICE_STREAK.clear(); ex._NO_PRICE_LAST_ALERT.clear()
    for _ in range(2):
        ex._note_no_price("ABC", "REAL")
    assert sent == []
    ex._note_no_price("ABC", "REAL")
    assert len(sent) == 1 and "ABC" in sent[0]
    for _ in range(5):
        ex._note_no_price("ABC", "REAL")
    assert len(sent) == 1                                                       # cool-down


def test_no_price_alert_can_be_switched_off(monkeypatch):
    from exit_engine import exit as ex
    sent = []
    monkeypatch.setattr(ex, "notify_sync", lambda text: sent.append(text))
    monkeypatch.setenv("EXIT_NO_PRICE_ALERT_CYCLES", "0")
    ex._NO_PRICE_STREAK.clear(); ex._NO_PRICE_LAST_ALERT.clear()
    for _ in range(20):
        ex._note_no_price("XYZ", "REAL")
    assert sent == []


def test_blank_env_uses_default(monkeypatch):
    from exit_engine import exit as ex
    monkeypatch.setenv("EXIT_NO_PRICE_ALERT_CYCLES", "  ")
    assert ex._env_int_blank_safe("EXIT_NO_PRICE_ALERT_CYCLES", 6) == 6
