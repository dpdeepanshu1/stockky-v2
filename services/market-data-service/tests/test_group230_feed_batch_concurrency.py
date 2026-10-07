"""group230 (log review item 3): after the first (held-positions) batch the feed polls FEED_BATCH_CONCURRENCY batches
at a time, retries a batch once on ConnectError, and still stops at once when the first batch trips the cooldown."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx
import pytest

import angelone_budget as b
from test_group211_angelone_budget import (  # noqa: F401  (fixtures + harness reused)
    _clean, feed, _Session, _one_cycle,
)
import test_group211_angelone_budget as t211


def test_default_concurrency_is_two(feed):
    assert feed.FEED_BATCH_CONCURRENCY == 2


def test_all_tokens_are_still_polled_exactly_once(feed, monkeypatch):
    sess = _one_cycle(feed, monkeypatch, 9)
    assert sorted(t for ts, _ in sess.calls for t in ts) == sorted(str(i) for i in range(1, 10))
    assert len(sess.calls) == 5                                   # 9 tokens, batches of 2


def test_batches_after_the_first_overlap(feed, monkeypatch):
    state = {"now": 0, "peak": 0}

    class _Slow(_Session):
        async def get_quotes_batch(self, exchange, tokens, lane=None):
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            await asyncio.sleep(0.05)
            state["now"] -= 1
            return await super().get_quotes_batch(exchange, tokens, lane=lane)

    monkeypatch.setattr(t211, "_Session", _Slow)
    _one_cycle(feed, monkeypatch, 9)
    assert state["peak"] == 2


def test_concurrency_one_restores_the_sequential_walk(feed, monkeypatch):
    monkeypatch.setattr(feed, "FEED_BATCH_CONCURRENCY", 1)
    state = {"now": 0, "peak": 0}

    class _Slow(_Session):
        async def get_quotes_batch(self, exchange, tokens, lane=None):
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            await asyncio.sleep(0.02)
            state["now"] -= 1
            return await super().get_quotes_batch(exchange, tokens, lane=lane)

    monkeypatch.setattr(t211, "_Session", _Slow)
    _one_cycle(feed, monkeypatch, 9)
    assert state["peak"] == 1


def test_a_connect_error_is_retried_once(feed, monkeypatch):
    seen = {"n": 0}

    class _Flaky(_Session):
        async def get_quotes_batch(self, exchange, tokens, lane=None):
            seen["n"] += 1
            if seen["n"] == 2:                                      # the second request only
                raise httpx.ConnectError("boom")
            return await super().get_quotes_batch(exchange, tokens, lane=lane)

    monkeypatch.setattr(t211, "_Session", _Flaky)
    _one_cycle(feed, monkeypatch, 5)
    assert seen["n"] == 4                                           # 3 batches + 1 retry


def test_a_second_connect_error_is_not_retried_again(feed, monkeypatch, caplog):
    class _Down(_Session):
        async def get_quotes_batch(self, exchange, tokens, lane=None):
            self.calls.append((list(tokens), lane))
            raise httpx.ConnectError("down")

    monkeypatch.setattr(t211, "_Session", _Down)
    with caplog.at_level("WARNING"):
        sess = _one_cycle(feed, monkeypatch, 2)
    assert len(sess.calls) == 2                                      # one batch, tried twice, then given up
    assert any("quote batch" in r.getMessage() and "failed" in r.getMessage() for r in caplog.records)


def test_a_cooldown_on_the_first_batch_still_ends_the_cycle(feed, monkeypatch):
    class _Tripping(_Session):
        async def get_quotes_batch(self, exchange, tokens, lane=None):
            self.calls.append((list(tokens), lane))
            b.trip("quote(batch)")
            return []

    monkeypatch.setattr(t211, "_Session", _Tripping)
    sess = _one_cycle(feed, monkeypatch, 9)
    assert len(sess.calls) == 1
