"""group251 (item 1 of the 2026-10-08 open-market log): a getCandleData 403 is followed by a gradual refill, not a burst.

Log: candle trip #1, then trip #2 about a minute later. The candle bucket (1.5/s, burst 3) had refilled completely by the end of
the 30 s cooldown while the /history callers shed during it were still queued, so they all fired together. Now:
  * the candle bucket default is 1.0/s, burst 2 (env RL_ANGELONE_CANDLE_RPS / _BURST still override);
  * a candle trip empties the bucket and holds the refill until the cooldown ends, then refills at half rate for
    ANGELONE_CANDLE_SLOWDOWN_S (600) more seconds (ANGELONE_CANDLE_SLOWDOWN_FACTOR 0.5; _S=0 or factor>=1 = off);
  * quote buckets are never touched.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import angelone_budget as b
import rate_limiter as rl


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("RL_ANGELONE_CANDLE_RPS", "RL_ANGELONE_CANDLE_BURST", "ANGELONE_CANDLE_SLOWDOWN_S",
              "ANGELONE_CANDLE_SLOWDOWN_FACTOR", "ANGELONE_SPLIT_COOLDOWN", "ANGELONE_BUDGET"):
        monkeypatch.delenv(k, raising=False)
    rl._buckets.pop("angelone_candle", None)
    rl._buckets.pop("angelone_quote", None)
    b._reset()
    yield
    rl._buckets.pop("angelone_candle", None)
    rl._buckets.pop("angelone_quote", None)
    b._reset()


def test_candle_bucket_defaults_are_lower():
    assert rl._cfg("angelone_candle") == (1.0, 2)
    assert rl._cfg("angelone_quote") == (5.0, 8)   # unchanged


def test_env_still_overrides_candle_bucket(monkeypatch):
    monkeypatch.setenv("RL_ANGELONE_CANDLE_RPS", "2.5")
    monkeypatch.setenv("RL_ANGELONE_CANDLE_BURST", "4")
    assert rl._cfg("angelone_candle") == (2.5, 4)


def test_slow_down_empties_and_holds_the_bucket():
    bk = rl._get_bucket("angelone_candle")
    assert bk.tokens == 2.0
    rl.slow_down("angelone_candle", hold_s=30.0, factor=0.5, slow_s=600.0)
    tokens, cap = rl.bucket_level("angelone_candle")
    assert tokens == 0.0 and cap == 2.0
    assert rl.would_block("angelone_candle") is True
    t0 = time.time()
    assert rl.try_acquire("angelone_candle", max_wait=0.2) is False   # nothing accrues during the hold
    assert time.time() - t0 < 1.5


def test_refill_after_hold_runs_at_half_rate(monkeypatch):
    rl.slow_down("angelone_candle", hold_s=30.0, factor=0.5, slow_s=600.0)
    bk = rl._get_bucket("angelone_candle")
    base = bk.updated                       # = end of the hold
    monkeypatch.setattr(rl.time, "time", lambda: base + 4.0)
    tokens, _cap = rl.bucket_level("angelone_candle")
    assert tokens == pytest.approx(2.0)     # 4 s * (1.0 * 0.5) = 2 tokens = full burst of 2
    monkeypatch.setattr(rl.time, "time", lambda: base + 1.0)
    tokens, _cap = rl.bucket_level("angelone_candle")
    assert tokens == pytest.approx(0.5)     # NOT 1.0 (the normal rate)


def test_hold_blocks_refill_before_it_ends(monkeypatch):
    rl.slow_down("angelone_candle", hold_s=30.0, factor=0.5, slow_s=600.0)
    now = time.time()
    monkeypatch.setattr(rl.time, "time", lambda: now + 29.0)
    tokens, _ = rl.bucket_level("angelone_candle")
    assert tokens == 0.0


def test_full_rate_returns_after_the_slowdown_window(monkeypatch):
    rl.slow_down("angelone_candle", hold_s=30.0, factor=0.5, slow_s=600.0)
    bk = rl._get_bucket("angelone_candle")
    monkeypatch.setattr(rl.time, "time", lambda: bk.slow_until + 0.5)
    with bk.lock:
        assert bk._eff_rps(rl.time.time()) == 1.0
    assert rl.stats()["angelone_candle"]["effective_rps"] == 1.0


def test_snapshot_reports_effective_rate():
    rl.slow_down("angelone_candle", hold_s=1.0, factor=0.5, slow_s=600.0)
    assert rl.stats()["angelone_candle"]["effective_rps"] == 0.5


def test_second_trip_extends_never_shortens():
    rl.slow_down("angelone_candle", hold_s=30.0, factor=0.5, slow_s=600.0)
    first = rl._get_bucket("angelone_candle").slow_until
    rl.slow_down("angelone_candle", hold_s=60.0, factor=0.5, slow_s=600.0)
    assert rl._get_bucket("angelone_candle").slow_until > first


def test_factor_of_one_or_zero_slow_window_only_holds():
    rl.slow_down("angelone_candle", hold_s=30.0, factor=1.0, slow_s=600.0)
    bk = rl._get_bucket("angelone_candle")
    assert bk.slow_until == 0.0 and bk.tokens == 0.0


def test_candle_trip_slows_the_candle_bucket_only():
    rl._get_bucket("angelone_quote")
    dur = b.trip("getCandleData")
    assert dur == 30.0
    assert rl.bucket_level("angelone_candle")[0] == 0.0
    assert rl.stats()["angelone_candle"]["effective_rps"] == 0.5
    q_tokens, q_cap = rl.bucket_level("angelone_quote")
    assert q_tokens == q_cap                     # quote bucket untouched
    assert rl.stats()["angelone_quote"]["effective_rps"] == 5.0


def test_quote_trip_leaves_candle_bucket_alone():
    b.trip("quote")
    tokens, cap = rl.bucket_level("angelone_candle")
    assert tokens == cap


def test_late_403_while_cooling_does_not_reslow():
    b.trip("getCandleData")
    until = rl._get_bucket("angelone_candle").slow_until
    assert b.trip("getCandleData") == 0.0         # suppressed late answer
    assert rl._get_bucket("angelone_candle").slow_until == until


@pytest.mark.parametrize("env,val", [("ANGELONE_CANDLE_SLOWDOWN_S", "0"), ("ANGELONE_CANDLE_SLOWDOWN_FACTOR", "1")])
def test_switch_off(monkeypatch, env, val):
    monkeypatch.setenv(env, val)
    rl._get_bucket("angelone_candle")
    b.trip("getCandleData")
    tokens, cap = rl.bucket_level("angelone_candle")
    assert tokens == cap and rl.stats()["angelone_candle"]["effective_rps"] == 1.0


@pytest.mark.parametrize("val", ["", "  ", "abc", "nan"])
def test_blank_or_bad_env_falls_back_to_defaults(monkeypatch, val):
    monkeypatch.setenv("ANGELONE_CANDLE_SLOWDOWN_S", val)
    monkeypatch.setenv("ANGELONE_CANDLE_SLOWDOWN_FACTOR", val)
    b.trip("getCandleData")
    assert rl.stats()["angelone_candle"]["effective_rps"] == 0.5


def test_budget_off_means_no_trip_no_slowdown(monkeypatch):
    monkeypatch.setenv("ANGELONE_BUDGET", "0")
    rl._get_bucket("angelone_candle")
    assert b.trip("getCandleData") == 0.0
    tokens, cap = rl.bucket_level("angelone_candle")
    assert tokens == cap


def test_slow_down_never_raises(monkeypatch):
    monkeypatch.setattr(rl, "_get_bucket", lambda p: (_ for _ in ()).throw(RuntimeError("x")))
    rl.slow_down("angelone_candle", 1.0, 0.5, 10.0)
