"""group 226 (item 5 of the 2026-10-07 list): an empty Hot Picks answer flagged "warming" is re-polled, not read as
"no catalysts" - otherwise the first watchlist refresh after a boot falls to Tier 2/3 and loses a whole cycle."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import watchlist_engine.sources as src  # noqa: E402
from test_group177_watchlist_tier1_reasons import _Resp, _breaker, run, HOT  # noqa: E402,F401

LOGGER = "real-trade-watchlist-sources"
WARMING = {"ok": True, "warming": True, "news_driven": [], "results_driven": [], "bulk_insider_driven": []}


class _SeqClient:
    """/stockky-hot answers the given sequence (last one repeats); the IPO list is empty."""

    def __init__(self, hot_seq):
        self.seq = list(hot_seq)
        self.hot_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def get(self, url, *a, **k):
        if not url.endswith("/stockky-hot"):          # IPO list and the Tier 2 events feed: empty
            return _Resp({})
        self.hot_calls += 1
        item = self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]
        if isinstance(item, BaseException):
            raise item
        return _Resp(item)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("WATCHLIST_TIER1_IPO_OPTIONAL", raising=False)
    monkeypatch.setenv("WATCHLIST_TIER1_WARMUP_WAIT_S", "0")
    monkeypatch.setenv("WATCHLIST_TIER1_WARMUP_RETRIES", "3")
    monkeypatch.setenv("WATCHLIST_TIER1_WARMUP_MIN_GAP_S", "120")
    src._warm_wait_last[0] = -1e9
    yield
    src._warm_wait_last[0] = -1e9


def _fetch(client, tier3=None):
    with patch("httpx.AsyncClient", return_value=client), \
         patch.object(src, "api_gateway_breaker", _breaker()), \
         patch.object(src, "event_service_breaker", _breaker()), \
         patch.object(src, "save_snapshot"), \
         patch.object(src, "load_snapshot", return_value=None), \
         patch.object(src, "_tier3_volume_shock", new=AsyncMock(return_value=tier3 or [])):
        return run(src.fetch_watchlist_candidates(object(), "DEMO"))


def test_warming_answer_is_repolled_until_hot_picks_arrive(caplog):
    c = _SeqClient([WARMING, WARMING, HOT])
    with caplog.at_level(logging.INFO, logger=LOGGER):
        out = _fetch(c)
    assert [x["symbol"] for x in out] == ["TCS"]
    assert c.hot_calls == 3                                  # first answer + 2 re-polls
    assert "ready after re-poll 2/3" in caplog.text


def test_still_warming_after_every_retry_falls_through_to_tier2_and_3(caplog):
    c = _SeqClient([WARMING])
    t3 = [{"symbol": "ABC", "catalyst_type": "volume_shock"}]
    with caplog.at_level(logging.INFO, logger=LOGGER):
        out = _fetch(c, tier3=t3)
    assert out == t3 and c.hot_calls == 4                    # 1 + 3 re-polls
    assert "still warming after 3 re-poll" in caplog.text


def test_a_non_warming_empty_answer_is_not_repolled():
    c = _SeqClient([{"news_driven": [], "results_driven": [], "bulk_insider_driven": []}])
    _fetch(c)
    assert c.hot_calls == 1


def test_a_warming_answer_that_already_lists_stale_picks_is_used_as_is():
    c = _SeqClient([{**HOT, "warming": True, "stale": True}])
    out = _fetch(c)
    assert [x["symbol"] for x in out] == ["TCS"] and c.hot_calls == 1


def test_retries_zero_switches_it_off(monkeypatch):
    monkeypatch.setenv("WATCHLIST_TIER1_WARMUP_RETRIES", "0")
    c = _SeqClient([WARMING])
    _fetch(c)
    assert c.hot_calls == 1


def test_only_one_waiting_spell_per_gap(monkeypatch):
    c = _SeqClient([WARMING])
    _fetch(c)
    first = c.hot_calls
    c2 = _SeqClient([WARMING])
    _fetch(c2)
    assert first == 4 and c2.hot_calls == 1                  # second cycle inside the gap does not wait again


def test_a_failing_repoll_gives_up_the_wait_and_never_raises(caplog):
    c = _SeqClient([WARMING, RuntimeError("boom")])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        out = _fetch(c)
    assert out == [] and c.hot_calls == 2
    assert "re-poll 1/3 failed" in caplog.text


def test_helpers_are_defensive(monkeypatch):
    assert src._hot_is_warming_empty(None) is False
    assert src._hot_is_warming_empty({"hot_picks": "x"}) is False
    assert src._hot_is_warming_empty({"hot_picks": WARMING}) is True
    monkeypatch.setenv("WATCHLIST_TIER1_WARMUP_WAIT_S", "garbage")
    assert src._env_num("WATCHLIST_TIER1_WARMUP_WAIT_S", 8.0) == 8.0
    monkeypatch.setenv("WATCHLIST_TIER1_WARMUP_WAIT_S", "-3")
    assert src._env_num("WATCHLIST_TIER1_WARMUP_WAIT_S", 8.0) == 8.0
    assert run(src._wait_for_hot_picks({"hot_picks": HOT})) == {"hot_picks": HOT}   # not warming: returned untouched
