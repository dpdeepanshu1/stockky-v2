"""group320: technical/main.py's is_market_open() honours the price-based closed-market verdict (fail-open)."""
import datetime as _dt
import os
import sys

import pytest

_TECH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "technical")
if _TECH not in sys.path:
    sys.path.insert(0, _TECH)

import main as tm  # noqa: E402  (technical/main.py, same import as test_technical_main.py)
import session_proof_client as spc  # noqa: E402


def _fake_now(monkeypatch, y, m, d, hh, mm):
    real = _dt.datetime(y, m, d, hh, mm, tzinfo=_dt.timezone(_dt.timedelta(hours=5, minutes=30)))

    class FakeDT:
        @staticmethod
        def now(tz=None):
            return real

    monkeypatch.setattr(tm, "datetime", FakeDT)


@pytest.fixture(autouse=True)
def _clean():
    spc.reset_state()
    yield
    spc.reset_state()


def test_open_without_verdict(monkeypatch):
    _fake_now(monkeypatch, 2026, 10, 12, 11, 0)
    assert tm.is_market_open() is True


def test_closed_verdict_closes_the_day_and_uses_the_long_ttl(monkeypatch):
    _fake_now(monkeypatch, 2026, 10, 12, 11, 0)
    spc.set_verdict("2026-10-12", True)
    assert tm.is_market_open() is False
    assert tm.get_cache_ttl() == 21600


def test_verdict_for_another_day_is_ignored(monkeypatch):
    _fake_now(monkeypatch, 2026, 10, 12, 11, 0)
    spc.set_verdict("2026-10-09", True)
    assert tm.is_market_open() is True
