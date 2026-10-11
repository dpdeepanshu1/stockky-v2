"""group319: tz_utils.is_market_open_ist honours market-data's price-based 'closed today' verdict (fail-open)."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import session_proof_client as spc
import tz_utils

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def _clean():
    spc.reset_state()
    yield
    spc.reset_state()


class _FixedNow(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 12, 10, 30, tzinfo=IST).astimezone(tz) if tz else cls(2026, 10, 12, 10, 30, tzinfo=IST)


def test_verdict_closes_only_the_matching_day():
    spc.set_verdict("2026-10-12", True)
    assert spc.closed_for_day("2026-10-12", refresh=False) is True
    assert spc.closed_for_day("2026-10-13", refresh=False) is False


def test_default_is_fail_open():
    assert spc.closed_for_day("2026-10-12", refresh=False) is False


def test_disabled_ignores_verdict(monkeypatch):
    spc.set_verdict("2026-10-12", True)
    monkeypatch.setenv("SESSION_PROOF_CLIENT_ENABLED", "false")
    assert spc.closed_for_day("2026-10-12", refresh=False) is False


def test_is_market_open_ist_blocked_by_verdict(monkeypatch):
    monkeypatch.setattr(tz_utils, "datetime", _FixedNow)
    assert tz_utils.is_market_open_ist() is True            # Monday 10:30 IST, not a listed holiday
    spc.set_verdict("2026-10-12", True)
    assert tz_utils.is_market_open_ist() is False


def test_explicit_now_ignores_verdict():
    spc.set_verdict("2026-10-12", True)
    when = datetime(2026, 10, 12, 10, 30, tzinfo=IST)
    assert tz_utils.is_market_open_ist(when) is True


def test_failed_fetch_never_raises_and_backs_off(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_URL", "http://127.0.0.1:9")
    spc._fetch()
    assert spc.closed_for_day("2026-10-12", refresh=False) is False
    assert spc._cache["inflight"] is False
