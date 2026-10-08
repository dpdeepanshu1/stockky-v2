"""group243: the dashboard price path (Candidates / Positions / Orders tabs) prices its symbols with one chunked
POST /quotes/bulk first instead of GET /live-quote + GET /quote per symbol.

No network: _bulk_ticks and get_quote are replaced. Run from services/real-trade-service:
    python3 -m pytest tests/test_group243_display_prices_bulk_first.py -q
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def feed(monkeypatch):
    import market_feed.feed as f
    f.clear_display_price_cache()
    monkeypatch.setattr(f, "_market_open_now", lambda: True)
    monkeypatch.setattr(f, "FEED_DISPLAY_BULK", True)
    monkeypatch.setattr(f, "FEED_DISPLAY_BULK_MIN_SYMBOLS", 8)
    monkeypatch.setattr(f, "FEED_DISPLAY_LEFTOVER_MAX", 10)
    calls = {"bulk": [], "quote": []}
    state = {"priced": {}, "failed": 0, "raise": None, "quote_prices": {}}

    async def fake_bulk(client, symbols, *, timeout=None, max_age_s=None, stats=None, schedule_atr=True):
        calls["bulk"].append({"symbols": list(symbols), "max_age_s": max_age_s, "schedule_atr": schedule_atr})
        if state["raise"]:
            raise state["raise"]
        if stats is not None:
            stats["failed"] = state["failed"]
        out = {}
        for s in symbols:
            p = state["priced"].get(s)
            if p:
                out[f._clean_sym(s)] = f.Tick(symbol=f._clean_sym(s), price=p, as_of=datetime.now(timezone.utc),
                                              atr=None, source="bulk(test)")
        return out

    async def fake_get_quote(client, sym, *, for_display=False, timeout_scale=1.0, skip_live_quote=False):
        calls["quote"].append({"sym": sym, "for_display": for_display, "skip_live": skip_live_quote})
        p = state["quote_prices"].get(sym)
        return f.Tick(symbol=sym, price=p, as_of=datetime.now(timezone.utc), atr=None, source="t") if p else None

    monkeypatch.setattr(f, "_bulk_ticks", fake_bulk)
    monkeypatch.setattr(f, "get_quote", fake_get_quote)
    yield f, calls, state
    f.clear_display_price_cache()


def _syms(n, prefix="S"):
    return [f"{prefix}{i}" for i in range(n)]


def test_forty_candidates_are_priced_by_one_bulk_call_and_no_per_symbol_call(feed):
    f, calls, state = feed
    syms = _syms(40)
    state["priced"] = {s: 100.0 + i for i, s in enumerate(syms)}
    out = _run(f.get_display_prices(syms))
    assert len(out) == 40 and out["S3"] == 103.0
    assert len(calls["bulk"]) == 1 and calls["bulk"][0]["symbols"] == syms
    assert calls["quote"] == []


def test_bulk_for_display_never_schedules_atr_refreshes(feed):
    f, calls, state = feed
    state["priced"] = {s: 1.0 for s in _syms(10)}
    _run(f.get_display_prices(_syms(10)))
    assert calls["bulk"][0]["schedule_atr"] is False


def test_open_market_uses_the_display_age_limit_closed_uses_the_preview_limit(feed, monkeypatch):
    f, calls, state = feed
    _run(f.get_display_prices(_syms(10)))
    assert calls["bulk"][0]["max_age_s"] == f.FEED_DISPLAY_BULK_MAX_AGE_S
    f.clear_display_price_cache()
    monkeypatch.setattr(f, "_market_open_now", lambda: False)
    _run(f.get_display_prices(_syms(10)))
    assert calls["bulk"][1]["max_age_s"] == f.FEED_PREVIEW_BULK_MAX_AGE_S


def test_leftovers_go_per_symbol_without_live_quote_and_are_capped(feed):
    f, calls, state = feed
    syms = _syms(40)
    state["priced"] = {s: 5.0 for s in syms[:20]}            # bulk misses 20
    state["quote_prices"] = {s: 7.0 for s in syms}
    out = _run(f.get_display_prices(syms))
    assert len(calls["quote"]) == 10                           # FEED_DISPLAY_LEFTOVER_MAX
    assert all(c["skip_live"] and c["for_display"] for c in calls["quote"])
    assert len(out) == 30 and out["S25"] == 7.0 and "S35" not in out


def test_unattempted_leftovers_are_not_remembered_as_misses(feed):
    f, calls, state = feed
    syms = _syms(40)
    state["priced"] = {s: 5.0 for s in syms[:20]}
    state["quote_prices"] = {s: 7.0 for s in syms}
    _run(f.get_display_prices(syms))
    calls["quote"].clear()
    out = _run(f.get_display_prices(syms))                     # next poll: the 10 not attempted are retried
    assert {c["sym"] for c in calls["quote"]} <= set(syms[30:])
    assert len(calls["quote"]) == 10 and len(out) == 40


def test_repeat_poll_within_ttl_makes_no_calls(feed):
    f, calls, state = feed
    state["priced"] = {s: 1.0 for s in _syms(12)}
    _run(f.get_display_prices(_syms(12)))
    _run(f.get_display_prices(_syms(12)))
    assert len(calls["bulk"]) == 1 and calls["quote"] == []


def test_small_batches_skip_bulk_and_keep_the_old_path(feed):
    f, calls, state = feed
    state["quote_prices"] = {s: 9.0 for s in _syms(5)}
    out = _run(f.get_display_prices(_syms(5)))
    assert calls["bulk"] == [] and len(calls["quote"]) == 5 and len(out) == 5
    assert not any(c["skip_live"] for c in calls["quote"])    # live-quote still tried, as before


def test_failed_bulk_falls_back_to_the_full_per_symbol_path(feed):
    f, calls, state = feed
    state["failed"] = 1                                        # every chunk failed, nothing answered
    state["quote_prices"] = {s: 3.0 for s in _syms(12)}
    out = _run(f.get_display_prices(_syms(12)))
    assert len(calls["quote"]) == 12 and len(out) == 12        # not capped: bulk told us nothing
    assert not any(c["skip_live"] for c in calls["quote"])


def test_bulk_exception_is_swallowed(feed):
    f, calls, state = feed
    state["raise"] = RuntimeError("boom")
    state["quote_prices"] = {s: 3.0 for s in _syms(9)}
    assert len(_run(f.get_display_prices(_syms(9)))) == 9


def test_switch_off_restores_per_symbol(feed, monkeypatch):
    f, calls, state = feed
    monkeypatch.setattr(f, "FEED_DISPLAY_BULK", False)
    state["quote_prices"] = {s: 3.0 for s in _syms(12)}
    _run(f.get_display_prices(_syms(12)))
    assert calls["bulk"] == [] and len(calls["quote"]) == 12


def test_symbol_neither_bulk_nor_quote_can_price_is_remembered_briefly(feed):
    f, calls, state = feed
    syms = _syms(10)
    state["priced"] = {s: 1.0 for s in syms[:9]}
    out1 = _run(f.get_display_prices(syms))
    n = len(calls["quote"])
    out2 = _run(f.get_display_prices(syms))
    assert out1 == out2 and "S9" not in out1 and len(calls["quote"]) == n
