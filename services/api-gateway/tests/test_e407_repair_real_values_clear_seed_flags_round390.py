"""E407: a real PE / ROCE / sentiment fetched by repair drops a stale seed flag left on the row (same rule E391 set for RSI).
Without it the real value is written next to `*_seed: True`, and data_feed.merge_feed_payload then treats it as a seed that a later seed run may replace."""
from __future__ import annotations

from test_main_patch_single_stock_feed import FakeResp, env, go, stored  # noqa: F401  (env is a fixture)


def test_real_pe_and_roce_clear_stale_seed_flags(env):
    env.client.routes["/analyze/A"] = FakeResp(200, {"pe_ratio": 18.2, "roce": 31.4})
    go(env, "A", {"price": 100, "rsi": 50, "sentiment_score": 0.3, "pe_ratio_seed": True, "pe_seed": True, "roce_seed": True})
    row = stored(env)
    assert row["pe_ratio"] == 18.2 and row["roce"] == 31.4
    assert not any(k in row for k in ("pe_ratio_seed", "pe_seed", "roce_seed"))


def test_real_sentiment_clears_a_stale_seed_flag(env):
    env.client.routes["/analyze/A"] = FakeResp(200, {"news_score": 0.8})
    go(env, "A", {"price": 100, "rsi": 50, "pe_ratio": 20, "roce": 15, "sentiment_seed": True})
    row = stored(env)
    assert row["sentiment_score"] == 0.8 and "sentiment_seed" not in row


def test_when_the_fetch_fails_the_seed_is_still_flagged(env):
    go(env, "A", {"price": 100, "rsi": 50, "sentiment_score": 0.3})          # no route: nothing real comes back
    row = stored(env)
    assert row["pe_ratio"] == 22.5 and row["pe_ratio_seed"] is True and row["roce"] == 15.0 and row["roce_seed"] is True
