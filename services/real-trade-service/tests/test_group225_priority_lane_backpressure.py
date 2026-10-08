"""
group225 (items 1/2 of the 2026-10-07 list): open positions keep a price while market-data is saturated.

At 09:43 the six REAL positions hit ReadTimeout on bulk, /live-quote and /quote every exit cycle because the same
process also sent ~579 per-symbol watchlist lookups. Pinned here:
  * when the whole priority lane could not price a held symbol, its last good tick (<= FEED_PRIORITY_STALE_FALLBACK_S
    old by its own as_of) is returned, with the real as_of and a "stale_last_good(...)" source
  * older ticks, FEED_PRIORITY_STALE_FALLBACK_S=0 and never-priced symbols get no fallback
  * a priority-lane failure switches non-priority per-symbol leftovers off for FEED_BACKPRESSURE_S
  * one non-priority batch sends at most FEED_LEFTOVER_MAX per-symbol lookups (0 = no cap)
Everything upstream is faked (no sockets).
"""
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import market_feed.feed as f  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _tick(sym, price=50.0, source="t", age_s=0.0, naive=False):
    as_of = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    if naive:
        as_of = as_of.replace(tzinfo=None)
    return f.Tick(symbol=sym, price=price, as_of=as_of, atr=1.5, source=source, volume=10,
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
    monkeypatch.setattr(f.httpx, "AsyncClient", _Dummy)
    monkeypatch.setattr(f, "FEED_PRIORITY_BULK_FIRST", True)
    monkeypatch.setattr(f, "FEED_PRIORITY_SHARE_S", 3.0)
    monkeypatch.setattr(f, "FEED_PRIORITY_STALE_FALLBACK_S", 90.0)
    monkeypatch.setattr(f, "FEED_BACKPRESSURE_S", 20.0)
    monkeypatch.setattr(f, "FEED_LEFTOVER_MAX", 120)
    monkeypatch.setattr(f, "FEED_BULK_MIN_SYMBOLS", 25)
    calls = {"bulk": [], "single": [], "down": False}

    async def fake_bulk(client, symbols, *, timeout=None, max_age_s=None, stats=None):
        calls["bulk"].append(list(symbols))
        if stats is not None:
            stats.setdefault("failed", 0)
            stats.setdefault("reasons", {})
        if calls["down"] or calls.get("bulk_empty"):
            if stats is not None and calls["down"]:
                stats["failed"] += 1
            return {}
        return {f._clean_sym(s): _tick(f._clean_sym(s), 77.0, "bulk(t)") for s in symbols}

    async def fake_get_quote(client, symbol, **kw):
        calls["single"].append(symbol)
        if calls["down"]:
            return None
        return _tick(symbol, 50.0, "single")

    monkeypatch.setattr(f, "_bulk_ticks", fake_bulk)
    monkeypatch.setattr(f, "get_quote", fake_get_quote)
    yield calls
    f.clear_priority_share()


# ── last-good fallback ──────────────────────────────────────────────────────

def test_held_symbols_fall_back_to_their_last_good_tick_when_market_data_is_down(env):
    first = _run(f.get_quotes(["A", "B"], priority=True))
    assert first["A"].source == "bulk(t)"
    with f._PRIO_LOCK:
        f._PRIO_SHARED.clear()                                  # the 3 s share window has passed
        f._PRIO_BULK_OFF_UNTIL[0] = 0.0
    env["down"] = True
    out = _run(f.get_quotes(["A", "B"], priority=True))
    assert set(out) == {"A", "B"}
    assert out["A"].price == 77.0
    assert out["A"].source == "stale_last_good(bulk(t))"
    assert out["A"].as_of == first["A"].as_of                   # real age is kept, never refreshed
    assert (out["A"].atr, out["A"].volume, out["A"].day_high, out["A"].day_low, out["A"].prev_close) == (1.5, 10, 60.0, 40.0, 49.0)


def test_a_too_old_last_good_tick_is_not_served(env):
    f._prio_remember_last_good({"A": _tick("A", 77.0, "bulk(t)", age_s=200)})
    env["down"] = True
    assert _run(f.get_quotes(["A"], priority=True)) == {}


def test_naive_as_of_is_treated_as_utc(env):
    f._prio_remember_last_good({"A": _tick("A", 77.0, "bulk(t)", age_s=5, naive=True)})
    env["down"] = True
    out = _run(f.get_quotes(["A"], priority=True))
    assert out["A"].source.startswith("stale_last_good(")


def test_fallback_can_be_switched_off(env, monkeypatch):
    f._prio_remember_last_good({"A": _tick("A")})
    monkeypatch.setattr(f, "FEED_PRIORITY_STALE_FALLBACK_S", 0.0)
    env["down"] = True
    assert _run(f.get_quotes(["A"], priority=True)) == {}
    f._prio_remember_last_good({"B": _tick("B")})               # off -> nothing is remembered either
    assert "B" not in f._PRIO_LAST_GOOD
    assert f._prio_last_good_fallback(["A"]) == {}


def test_a_symbol_never_priced_has_no_fallback_and_empty_input_is_fine(env):
    env["down"] = True
    assert _run(f.get_quotes(["NEVER"], priority=True)) == {}
    assert f._prio_last_good_fallback([]) == {}
    f._prio_remember_last_good({})


def test_stale_ticks_are_not_remembered_or_shared_again(env):
    f._prio_remember_last_good({"A": _tick("A", 77.0, "stale_last_good(x)")})
    assert "A" not in f._PRIO_LAST_GOOD
    f._prio_remember_last_good({"A": _tick("A", 77.0, "bulk(t)", age_s=10)})
    env["down"] = True
    out = _run(f.get_quotes(["A"], priority=True))
    assert out["A"].source.startswith("stale_last_good(")
    assert "A" not in f._PRIO_SHARED                           # a fallback tick never enters the share cache


def test_the_last_good_store_is_bounded_and_cleared_by_clear_priority_share(env):
    f._prio_remember_last_good({f"S{i}": _tick(f"S{i}") for i in range(501)})
    f._prio_remember_last_good({"X": _tick("X")})
    assert list(f._PRIO_LAST_GOOD) == ["X"]
    f.clear_priority_share()
    assert f._PRIO_LAST_GOOD == {} and f._PRIO_DISTRESS_UNTIL[0] == 0.0


def test_a_symbol_the_lane_recovers_is_not_replaced_by_the_fallback(env):
    f._prio_remember_last_good({"A": _tick("A", 11.0, "old(t)", age_s=30)})
    out = _run(f.get_quotes(["A"], priority=True))
    assert out["A"].source == "bulk(t)" and out["A"].price == 77.0


# ── back-pressure on the non-priority batch ─────────────────────────────────

def test_priority_failure_switches_per_symbol_leftovers_off(env):
    env["down"] = True
    _run(f.get_quotes(["A"], priority=True))
    assert f._prio_in_distress()
    env["down"] = False
    env["bulk_empty"] = True                                   # bulk answers nothing -> 40 leftovers
    env["single"].clear()
    out = _run(f.get_quotes([f"S{i}" for i in range(40)]))
    assert out == {} and env["single"] == []                   # the lane is protected


def test_leftovers_run_again_once_the_distress_window_ends(env):
    with f._PRIO_LOCK:
        f._PRIO_DISTRESS_UNTIL[0] = time.monotonic() - 1
    assert not f._prio_in_distress()
    env["bulk_empty"] = True
    out = _run(f.get_quotes([f"S{i}" for i in range(40)]))
    assert len(out) == 40 and len(env["single"]) == 40


def test_backpressure_zero_never_marks_distress(env, monkeypatch):
    monkeypatch.setattr(f, "FEED_BACKPRESSURE_S", 0.0)
    env["down"] = True
    _run(f.get_quotes(["A"], priority=True))
    assert not f._prio_in_distress()


def test_one_batch_sends_at_most_leftover_max_per_symbol_lookups(env, monkeypatch):
    monkeypatch.setattr(f, "FEED_LEFTOVER_MAX", 30)
    env["bulk_empty"] = True
    out = _run(f.get_quotes([f"S{i}" for i in range(100)]))
    assert len(env["single"]) == 30 and len(out) == 30


def test_leftover_cap_zero_means_no_cap(env, monkeypatch):
    monkeypatch.setattr(f, "FEED_LEFTOVER_MAX", 0)
    env["bulk_empty"] = True
    out = _run(f.get_quotes([f"S{i}" for i in range(100)]))
    assert len(out) == 100


def test_small_entry_batches_are_never_starved_by_the_distress_window(env):
    env["down"] = True
    _run(f.get_quotes(["A"], priority=True))
    assert f._prio_in_distress()
    env["down"] = False
    env["single"].clear()
    out = _run(f.get_quotes([f"S{i}" for i in range(20)]))     # a cycle's <=20 entry candidates
    # group246: a 20-symbol batch is priced by one bulk call now; it is still not skipped during the window
    assert len(out) == 20 and len(env["single"]) == 0
    # ... and when bulk answers nothing, its leftovers still go through the per-symbol path (never starved)
    env["bulk_empty"] = True
    env["single"].clear()
    out = _run(f.get_quotes([f"S{i}" for i in range(20)]))
    assert len(out) == 20 and len(env["single"]) == 20


def test_distress_is_not_marked_when_the_bulk_fallback_recovers_every_held_symbol(env):
    env["bulk_empty"] = False
    calls = {"n": 0}
    orig = f._bulk_ticks

    async def flaky_bulk(client, symbols, *, timeout=None, max_age_s=None, stats=None):
        calls["n"] += 1
        if calls["n"] == 1:                                    # bulk-first misses, per-symbol also fails, last resort works
            if stats is not None:
                stats.setdefault("failed", 0); stats.setdefault("reasons", {})
            return {}
        return await orig(client, symbols, timeout=timeout, max_age_s=max_age_s, stats=stats)

    async def no_single(client, symbol, **kw):
        return None

    import pytest as _p
    mp = _p.MonkeyPatch()
    mp.setattr(f, "_bulk_ticks", flaky_bulk)
    mp.setattr(f, "get_quote", no_single)
    try:
        out = _run(f.get_quotes(["A"], priority=True))
    finally:
        mp.undo()
    assert out["A"].price == 77.0 and not f._prio_in_distress()
