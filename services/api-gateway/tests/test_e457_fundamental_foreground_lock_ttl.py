"""E457 (round 436): the foreground fundamentals refresh takes a lock that outlives the 35 s upstream call (was 5 s), like the background one (E419)."""
from __future__ import annotations

from test_main_scoring import FKEY, feed, gw, kv, locks, run  # noqa: F401


def test_the_foreground_lock_outlives_the_upstream_timeout(feed, kv, locks, monkeypatch):
    async def upstream(symbol, client, cache_key):
        return {"up": 1}, False

    monkeypatch.setattr(gw, "_refresh_fundamental_upstream", upstream)
    assert run(gw._fetch_fundamental_cached("TCS", "CLIENT")) == ({"up": 1}, False)
    assert locks.acquired == [("TCS", gw._FUNDAMENTAL_FG_LOCK_TTL_SEC)]
    assert gw._FUNDAMENTAL_FG_LOCK_TTL_SEC > 35          # the timeout inside _refresh_fundamental_upstream
    assert locks.released == ["TCS"]                     # a normal call still frees it as soon as it is done


def test_the_foreground_lock_is_released_when_the_upstream_call_raises(feed, kv, locks, monkeypatch):
    async def upstream(symbol, client, cache_key):
        raise RuntimeError("boom")

    monkeypatch.setattr(gw, "_refresh_fundamental_upstream", upstream)
    try:
        run(gw._fetch_fundamental_cached("TCS", "CLIENT"))
    except RuntimeError:
        pass
    assert locks.released == ["TCS"]


def test_a_second_request_while_the_lock_is_held_does_not_go_upstream(feed, kv, locks, monkeypatch):
    locks.acquire = False                                # somebody holds it
    calls = []

    async def upstream(symbol, client, cache_key):
        calls.append(symbol)
        return {"up": 1}, False

    monkeypatch.setattr(gw, "_refresh_fundamental_upstream", upstream)
    assert run(gw._fetch_fundamental_cached("TCS", "CLIENT")) == (None, True)
    assert calls == [] and locks.released == []
