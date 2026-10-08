"""group249 (2026-10-08 11:03 IST log, after group 248): ~138 watchlist symbols bulk could not price still went to
per-symbol GET /quote and ~100 ended in ReadTimeout. A caller that opts in (allow_stale=True - the watchlist trigger) now
reuses the last tick the non-priority path priced for such a symbol, if it is at most FEED_LEFTOVER_STALE_S old."""
import asyncio
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from market_feed import feed as f
from tests.test_group225_priority_lane_backpressure import _tick


def _run(coro):
    return asyncio.run(coro)


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
    monkeypatch.setattr(f, "FEED_BULK_MIN_SYMBOLS", 25)
    monkeypatch.setattr(f, "FEED_LEFTOVER_MAX", 120)
    monkeypatch.setattr(f, "FEED_BULK_RETRY_FAILED", False)     # keep these tests about the last-good step only
    st = {"single": [], "answers": 40}

    async def fake_bulk(client, symbols, *, timeout=None, max_age_s=None, stats=None, schedule_atr=True,
                        chunk_size=None):
        if stats is not None:
            stats.setdefault("failed", 0)
            stats.setdefault("reasons", {})
        ok = list(symbols)[: st["answers"]]
        lost = list(symbols)[st["answers"]:]
        if lost and stats is not None:
            stats["failed"] += 1
            stats.setdefault("failed_symbols", []).extend(lost)
        return {f._clean_sym(s): _tick(f._clean_sym(s), 77.0, "bulk(t)") for s in ok}

    async def fake_get_quote(client, symbol, **kw):
        st["single"].append(symbol)
        return _tick(symbol, 50.0, "single")

    monkeypatch.setattr(f, "_bulk_ticks", fake_bulk)
    monkeypatch.setattr(f, "get_quote", fake_get_quote)
    yield st
    f.clear_priority_share()


def _syms(n=40):
    return [f"S{i}" for i in range(n)]


def _poll_then_lose(env, **kw):
    """Poll 1: every symbol priced by bulk (remembered). Poll 2: only 10 answered."""
    _run(f.get_quotes(_syms()))
    env["answers"] = 10
    return _run(f.get_quotes(_syms(), **kw))


def test_opt_in_caller_gets_last_good_ticks_instead_of_per_symbol_calls(env, caplog):
    with caplog.at_level("INFO"):
        out = _poll_then_lose(env, allow_stale=True)
    assert len(out) == 40 and env["single"] == []
    assert out["S39"].source == "stale_last_good(bulk(t))" and out["S0"].source == "bulk(t)"
    assert out["S39"].price == 77.0
    assert "30 unpriced symbol(s) served from their last good tick" in caplog.text
    assert "0 left for per-symbol lookups" in caplog.text


def test_default_caller_still_uses_the_per_symbol_path(env):
    out = _poll_then_lose(env)
    assert len(out) == 40 and len(env["single"]) == 30
    assert all(not out[s].source.startswith("stale_last_good") for s in out)


def test_a_last_good_tick_older_than_the_limit_is_not_used(env, monkeypatch):
    _run(f.get_quotes(_syms()))
    old = datetime.now(timezone.utc) - timedelta(seconds=f.FEED_LEFTOVER_STALE_S + 30)
    for t in f._WL_LAST_GOOD.values():
        t.as_of = old
    env["answers"] = 10
    out = _run(f.get_quotes(_syms(), allow_stale=True))
    assert len(env["single"]) == 30 and len(out) == 40
    assert not any(t.source.startswith("stale_last_good") for t in out.values())


def test_symbol_never_priced_before_goes_to_the_per_symbol_path(env):
    env["answers"] = 10
    out = _run(f.get_quotes(_syms(), allow_stale=True))      # no earlier poll: nothing remembered
    assert len(env["single"]) == 30 and len(out) == 40


def test_can_be_switched_off(env, monkeypatch):
    monkeypatch.setattr(f, "FEED_LEFTOVER_STALE_S", 0.0)
    out = _poll_then_lose(env, allow_stale=True)
    assert len(env["single"]) == 30 and len(out) == 40


def test_a_stale_served_tick_keeps_its_real_age_and_is_never_refreshed(env):
    _poll_then_lose(env, allow_stale=True)
    before = f._WL_LAST_GOOD["S39"].as_of
    env["answers"] = 10
    out = _run(f.get_quotes(_syms(), allow_stale=True))      # served from last good again
    assert out["S39"].as_of == before
    assert f._WL_LAST_GOOD["S39"].as_of == before and f._WL_LAST_GOOD["S39"].source == "bulk(t)"


def test_per_symbol_results_are_remembered_too(env):
    env["answers"] = 10
    _run(f.get_quotes(_syms()))                              # 30 priced by the per-symbol path
    assert f._WL_LAST_GOOD["S39"].source == "single"
    out = _run(f.get_quotes(_syms(), allow_stale=True))
    assert out["S39"].source == "stale_last_good(single)" and len(env["single"]) == 30    # no new per-symbol calls


def test_only_the_watchlist_trigger_opts_in():
    root = pathlib.Path(f.__file__).resolve().parent.parent
    entry = (root / "entry_engine" / "entry.py").read_text(encoding="utf-8")
    assert entry.count("allow_stale=True") == 1
    idx = entry.index("allow_stale=True")
    assert "watchlist" in entry[max(0, idx - 2500):idx].lower()
    for rel in ("exit_engine/exit.py", "main.py", "execution/auto_pilot.py", "portfolio/portfolio.py", "manual_engine.py"):
        assert "allow_stale" not in (root / rel).read_text(encoding="utf-8")
