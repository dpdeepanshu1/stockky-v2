"""group252 (item 2 of the 2026-10-08 open-market log): the held-position bulk call is hedged by the per-symbol lookups.

Log: `/quotes/bulk chunk of 6 failed: ReadTimeout` (the 4 s priority bulk timeout) and only then did the per-symbol path start,
so the six held prices - and the 8 s exit cycle - lost 4 s although market-data answered the per-symbol calls at once.
Now, when bulk has not answered within FEED_PRIORITY_HEDGE_S (1.5 s) the per-symbol lookups start alongside it; each
symbol takes whichever answer arrives first; a bulk call still unanswered counts as a failed bulk-first (30 s pause).
"""
import asyncio
import os
import sys
import time
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import market_feed.feed as f  # noqa: E402


def _tick(sym, price=50.0, source="t"):
    return f.Tick(symbol=sym, price=price, as_of=datetime.now(timezone.utc), atr=1.5, source=source, volume=10,
                  day_high=60.0, day_low=40.0, prev_close=49.0)


class _Dummy:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture()
def env(monkeypatch):
    f.clear_priority_share()
    f.clear_dead_symbols()
    f._PRIO_BULK_OFF_UNTIL[0] = 0.0
    monkeypatch.setattr(f.httpx, "AsyncClient", _Dummy)
    monkeypatch.setattr(f, "FEED_PRIORITY_BULK_FIRST", True)
    monkeypatch.setattr(f, "FEED_PRIORITY_SHARE_S", 0.0)
    monkeypatch.setattr(f, "FEED_PRIORITY_HEDGE_S", 0.2)
    monkeypatch.setattr(f, "FEED_PRIORITY_BULK_COOLDOWN_S", 30.0)
    c = {"bulk": [], "single": [], "bulk_delay": 0.0, "single_delay": 0.0, "bulk_fail": False, "bulk_cancelled": 0,
         "single_none": set()}

    async def fake_bulk(client, symbols, *, timeout=None, max_age_s=None, stats=None, **kw):
        c["bulk"].append(list(symbols))
        if stats is not None:
            stats.setdefault("failed", 0)
            stats.setdefault("reasons", {})
        try:
            await asyncio.sleep(c["bulk_delay"])
        except asyncio.CancelledError:
            c["bulk_cancelled"] += 1
            raise
        if c["bulk_fail"]:
            if stats is not None:
                stats["failed"] += 1
            return {}
        return {f._clean_sym(s): _tick(f._clean_sym(s), 77.0, "bulk(t)") for s in symbols}

    async def fake_get_quote(client, symbol, **kw):
        c["single"].append(symbol)
        await asyncio.sleep(c["single_delay"])
        if symbol in c["single_none"]:
            return None
        return _tick(f._clean_sym(symbol), 55.0, "single(t)")

    monkeypatch.setattr(f, "_bulk_ticks", fake_bulk)
    monkeypatch.setattr(f, "get_quote", fake_get_quote)
    return c


def _run(coro):
    return asyncio.run(coro)


SYMS = ["GENUSPOWER", "MPHASIS", "AURIONPRO"]


def test_fast_bulk_never_hedges(env):
    out = _run(f._priority_quotes(SYMS))
    assert env["single"] == []
    assert {t.price for t in out.values()} == {77.0}
    assert f._PRIO_BULK_OFF_UNTIL[0] == 0.0


def test_slow_bulk_starts_per_symbol_after_hedge_delay_and_does_not_wait_for_bulk(env):
    env["bulk_delay"] = 5.0          # would have timed out in real life
    t0 = time.monotonic()
    out = _run(f._priority_quotes(SYMS))
    took = time.monotonic() - t0
    assert took < 1.5, took          # NOT 5 s (nor the 4 s timeout)
    assert sorted(env["single"]) == sorted(SYMS)
    assert {t.price for t in out.values()} == {55.0}
    assert env["bulk_cancelled"] == 1
    assert f._PRIO_BULK_OFF_UNTIL[0] > time.monotonic() + 20   # bulk-first paused like a failed bulk


def test_hedge_not_started_when_bulk_answers_just_inside_the_delay(env):
    env["bulk_delay"] = 0.1
    out = _run(f._priority_quotes(SYMS))
    assert env["single"] == []
    assert {t.price for t in out.values()} == {77.0}


def test_bulk_arriving_after_hedge_but_before_per_symbol_finishes_wins_per_symbol(env):
    env["bulk_delay"] = 0.35
    env["single_delay"] = 2.0
    t0 = time.monotonic()
    out = _run(f._priority_quotes(SYMS))
    assert time.monotonic() - t0 < 1.5
    assert {t.price for t in out.values()} == {77.0}
    assert f._PRIO_BULK_OFF_UNTIL[0] == 0.0     # bulk answered: no pause
    assert sorted(env["single"]) == sorted(SYMS)


def test_per_symbol_prices_kept_when_bulk_later_fails_and_one_symbol_stays_unpriced(env):
    env["bulk_delay"] = 0.6
    env["bulk_fail"] = True
    env["single_none"] = {"MPHASIS"}            # per-symbol cannot price one symbol and bulk fails
    out = _run(f._priority_quotes(SYMS))
    assert out["GENUSPOWER"].price == 55.0 and out["AURIONPRO"].price == 55.0
    assert "MPHASIS" not in out
    assert f._PRIO_BULK_OFF_UNTIL[0] > time.monotonic() + 20


def test_uncovered_symbol_waits_for_the_pending_bulk_and_takes_its_price(env):
    env["bulk_delay"] = 0.6
    env["single_none"] = {"MPHASIS"}
    out = _run(f._priority_quotes(SYMS))
    assert out["MPHASIS"].price == 77.0 and out["GENUSPOWER"].price == 55.0
    assert f._PRIO_BULK_OFF_UNTIL[0] == 0.0


def test_symbol_failing_per_symbol_goes_to_last_resort_bulk_without_repeating_per_symbol(env):
    env["bulk_delay"] = 0.6
    env["bulk_fail"] = True
    env["single_none"] = {"MPHASIS"}
    # last-resort bulk (step 3) reuses the same fake; make it answer fast for that call only
    calls = {"n": 0}
    real = f._bulk_ticks

    async def bulk(client, symbols, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return await real(client, symbols, **kw)
        return {f._clean_sym(s): _tick(f._clean_sym(s), 66.0, "bulk2") for s in symbols}

    f._bulk_ticks = bulk
    try:
        out = _run(f._priority_quotes(SYMS))
    finally:
        f._bulk_ticks = real
    assert out["MPHASIS"].price == 66.0
    assert env["single"].count("MPHASIS") == 1   # per-symbol was not run a second time


def test_bulk_failure_answered_inside_hedge_delay_behaves_as_before(env):
    env["bulk_fail"] = True
    env["bulk_delay"] = 0.05
    out = _run(f._priority_quotes(SYMS))
    assert sorted(env["single"]) == sorted(SYMS)
    assert f._PRIO_BULK_OFF_UNTIL[0] > time.monotonic() + 20
    assert {t.price for t in out.values()} == {55.0}


def test_hedge_off_waits_for_bulk_as_before(env, monkeypatch):
    monkeypatch.setattr(f, "FEED_PRIORITY_HEDGE_S", 0.0)
    env["bulk_delay"] = 0.5
    t0 = time.monotonic()
    out = _run(f._priority_quotes(SYMS))
    assert time.monotonic() - t0 >= 0.5
    assert env["single"] == []
    assert {t.price for t in out.values()} == {77.0}


def test_bulk_first_paused_means_no_hedge_and_plain_per_symbol(env):
    f._PRIO_BULK_OFF_UNTIL[0] = time.monotonic() + 100
    out = _run(f._priority_quotes(SYMS))
    assert env["bulk"] == [] and sorted(env["single"]) == sorted(SYMS)
    assert {t.price for t in out.values()} == {55.0}


def test_hedged_results_are_shared_and_remembered_as_last_good(env, monkeypatch):
    monkeypatch.setattr(f, "FEED_PRIORITY_SHARE_S", 3.0)
    env["bulk_delay"] = 5.0
    _run(f._priority_quotes(SYMS))
    again = _run(f._priority_quotes(SYMS))
    assert len(env["single"]) == 3               # second call served from the shared ticks
    assert {t.price for t in again.values()} == {55.0}


def test_env_blank_safe():
    for raw, want in (("", 1.5), ("  ", 1.5), ("x", 1.5), ("-1", 1.5), ("0", 0.0), ("2.5", 2.5)):
        os.environ["FEED_PRIORITY_HEDGE_S"] = raw
        try:
            assert f._env_float("FEED_PRIORITY_HEDGE_S", 1.5) == want
        finally:
            os.environ.pop("FEED_PRIORITY_HEDGE_S", None)
