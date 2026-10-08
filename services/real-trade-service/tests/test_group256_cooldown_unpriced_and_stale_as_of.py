"""group256 (real-trade-service side): market-data answers GET /quote with source="cooldown_unpriced" (price None) while AngelOne's
quote cooldown runs and it has no recent cached price, and with source="stale_cooldown(<src>)" and the price's real fetched_at when it
has one. The first says nothing about the symbol, so it must not count towards the dead-symbol pause (group 160); the second must keep
its real age instead of being stamped with the time real-trade received it, and must never be remembered as a symbol's "last good" tick.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import market_feed.feed as f  # noqa: E402


class _R:
    def __init__(self, body, status=200):
        self._b, self.status_code, self.text = body, status, "x"

    def json(self):
        return self._b


class _Client:
    def __init__(self, body):
        self.body = body

    async def get(self, url, timeout=None):
        return _R(self.body)


@pytest.fixture
def noted(monkeypatch):
    n = {"no_data": [], "priced": []}
    monkeypatch.setattr(f, "_note_no_data", lambda s: n["no_data"].append(s))
    monkeypatch.setattr(f, "_note_priced", lambda s: n["priced"].append(s))
    monkeypatch.setattr(f, "_schedule_atr_refresh", lambda *a, **k: None)
    monkeypatch.setattr(f, "_cached_atr", lambda s: None)
    monkeypatch.setattr(f, "_market_open_now", lambda: False)
    return n


def _get(body):
    return asyncio.run(f.get_quote(_Client(body), "ABC", skip_live_quote=True))


def test_cooldown_unpriced_is_not_a_dead_symbol_miss(noted):
    assert _get({"symbol": "ABC", "price": None, "cmp": None, "source": "cooldown_unpriced"}) is None
    assert noted["no_data"] == []


def test_a_genuine_no_price_answer_still_counts(noted):
    assert _get({"symbol": "ABC", "price": None, "cmp": None, "source": "failed"}) is None
    assert noted["no_data"] == ["ABC"]


def test_stale_cooldown_keeps_the_real_age(noted):
    ts = (datetime.utcnow() - timedelta(seconds=100)).isoformat()
    t = _get({"symbol": "ABC", "price": 50.0, "source": "stale_cooldown(angelone_rest)", "fetched_at": ts})
    age = (datetime.now(timezone.utc) - t.as_of).total_seconds()
    assert 95 < age < 110 and t.source == "stale_cooldown(angelone_rest)" and t.price == 50.0


def test_a_normal_row_is_still_stamped_with_receipt_time(noted):
    ts = (datetime.utcnow() - timedelta(seconds=100)).isoformat()
    t = _get({"symbol": "ABC", "price": 50.0, "source": "yahoo_clean", "fetched_at": ts})
    assert (datetime.now(timezone.utc) - t.as_of).total_seconds() < 5


def test_as_of_helper_edge_cases():
    assert f._stale_cooldown_as_of({"source": "stale_cooldown(x)"}) is None
    assert f._stale_cooldown_as_of({"source": "stale_cooldown(x)", "fetched_at": "garbage"}) is None
    assert f._stale_cooldown_as_of({"source": "other", "fetched_at": "2026-10-08T08:00:00"}) is None
    z = f._stale_cooldown_as_of({"source": "stale_cooldown(x)", "fetched_at": "2026-10-08T08:00:00Z"})
    assert z.tzinfo is not None and z.hour == 8


def test_stale_cooldown_ticks_are_not_remembered_as_last_good():
    old = f.Tick(symbol="ABC", price=50.0, as_of=datetime.now(timezone.utc) - timedelta(seconds=150), atr=None,
                 source="stale_cooldown(angelone_rest)", volume=None, day_high=None, day_low=None, prev_close=None)
    good = f.Tick(symbol="ABC", price=51.0, as_of=datetime.now(timezone.utc), atr=None,
                  source="angelone_rest", volume=None, day_high=None, day_low=None, prev_close=None)
    f._WL_LAST_GOOD.pop("ABC", None)
    f._PRIO_LAST_GOOD.pop("ABC", None)
    f._wl_remember_last_good({"ABC": good})
    f._wl_remember_last_good({"ABC": old})
    f._prio_remember_last_good({"ABC": good})
    f._prio_remember_last_good({"ABC": old})
    assert f._WL_LAST_GOOD["ABC"].price == 51.0 and f._PRIO_LAST_GOOD["ABC"].price == 51.0
    f._WL_LAST_GOOD.pop("ABC", None)
    f._PRIO_LAST_GOOD.pop("ABC", None)
