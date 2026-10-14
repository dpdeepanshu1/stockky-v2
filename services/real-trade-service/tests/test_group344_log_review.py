"""group344 (13 Oct log review): watchlist parking, Tier 1 snapshot while Hot Picks warms, bounded pre-open warm-up, held-symbol feed check,
priority flag on the held-position quote call."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from entry_engine import entry, preopen
from market_feed import feed, held_coverage
from watchlist_engine import sources
from test_watchlist_trigger import make_row, tick


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setenv("WATCHLIST_MAX_CHASE_PCT", "0")
    entry._adverse_last_log.clear()
    entry._wl_parked.clear()
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    entry._wl_parked.clear()


# ------------------------------------------------------------------ #6 parking
def _quotes(monkeypatch, price, calls):
    async def _q(syms, **_kw):
        calls.append(sorted(syms))
        return {s: tick(price, s) for s in syms}
    monkeypatch.setattr(entry, "get_quotes", _q)


def test_price_blocked_row_is_parked_and_not_priced_next_cycle(db, monkeypatch):
    monkeypatch.setenv("WATCHLIST_PARK_S", "300")
    make_row(db, symbol="DROPPED", catalyst_price=100.0)
    make_row(db, symbol="FINE", catalyst_price=100.0)
    calls = []
    _quotes(monkeypatch, 89.0, calls)          # both 11% below the catalyst -> price-blocked
    t1 = run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert calls[0] == ["DROPPED", "FINE"] and t1["adverse"] == 2
    t2 = run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert len(calls) == 1                      # nothing left to price: no quote call at all
    assert t2["watchlist_checked"] == 0 and t2.get("parked") == 2


def test_parked_row_stays_active(db, monkeypatch):
    monkeypatch.setenv("WATCHLIST_PARK_S", "300")
    row = make_row(db, symbol="DROPPED", catalyst_price=100.0)
    _quotes(monkeypatch, 89.0, [])
    run(entry.evaluate_watchlist_entries(db, "DEMO"))
    db.refresh(row)
    assert row.status == "active"


def test_park_ends_and_row_is_checked_again(db, monkeypatch):
    monkeypatch.setenv("WATCHLIST_PARK_S", "300")
    row = make_row(db, symbol="DROPPED", catalyst_price=100.0)
    calls = []
    _quotes(monkeypatch, 89.0, calls)
    run(entry.evaluate_watchlist_entries(db, "DEMO"))
    entry._wl_parked[row.id] = 0.0               # long expired
    run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert len(calls) == 2


def test_park_switched_off_prices_every_cycle(db, monkeypatch):
    monkeypatch.setenv("WATCHLIST_PARK_S", "0")
    make_row(db, symbol="DROPPED", catalyst_price=100.0)
    calls = []
    _quotes(monkeypatch, 89.0, calls)
    run(entry.evaluate_watchlist_entries(db, "DEMO"))
    run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert len(calls) == 2


def test_only_price_reasons_are_parkable():
    assert entry._wl_is_parkable("price is 11.0% below catalyst (limit -5.0%)")
    assert entry._wl_is_parkable("price is 9.0% above catalyst")
    assert not entry._wl_is_parkable("no previous close yet")
    assert not entry._wl_is_parkable(None)


def test_in_band_row_is_never_parked(db, monkeypatch):
    monkeypatch.setenv("WATCHLIST_PARK_S", "300")
    monkeypatch.setenv("WATCHLIST_REQUEUE_COOLDOWN_S", "0")      # group345 parks a QUEUED row too; this test is about the price-park only
    make_row(db, symbol="OK", catalyst_price=100.0)
    calls = []
    _quotes(monkeypatch, 100.5, calls)
    run(entry.evaluate_watchlist_entries(db, "DEMO"))
    run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert len(calls) == 2 and not entry._wl_parked


# ------------------------------------------------------------------ #5 snapshot
def _payload(with_picks=True):
    hot = {"warming": not with_picks, "bulk_insider_driven": [{"symbol": "AAA"}] if with_picks else [],
           "results_driven": [], "news_driven": []}
    return {"hot_picks": hot}


def test_stamp_and_fresh_snapshot():
    snap = sources._stamp_snapshot(_payload())
    assert sources._snapshot_fresh_enough(snap, 8.0)


def test_snapshot_without_picks_or_stamp_is_not_used():
    assert not sources._snapshot_fresh_enough(sources._stamp_snapshot(_payload(False)), 8.0)
    assert not sources._snapshot_fresh_enough(_payload(), 8.0)
    assert not sources._snapshot_fresh_enough(None, 8.0)


def test_old_or_disabled_snapshot_is_not_used():
    snap = sources._stamp_snapshot(_payload())
    snap[sources._SNAP_STAMP] = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
    assert not sources._snapshot_fresh_enough(snap, 8.0)
    assert not sources._snapshot_fresh_enough(sources._stamp_snapshot(_payload()), 0.0)


def test_yesterdays_snapshot_is_not_used():
    snap = sources._stamp_snapshot(_payload())
    snap[sources._SNAP_STAMP] = (datetime.now(timezone.utc) - timedelta(days=1, minutes=1)).isoformat()
    assert not sources._snapshot_fresh_enough(snap, 48.0)


# ------------------------------------------------------------------ #3 warm-up budget
class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_warmup_stops_at_budget(monkeypatch):
    from entry_engine import event_risk, prev_day, vix_gate
    clk = _Clock()
    seen = []
    monkeypatch.setattr(event_risk, "lookup", lambda s: (seen.append(s), setattr(clk, "t", clk.t + 20.0)))
    monkeypatch.setattr(prev_day, "prefetch", lambda s: None)
    monkeypatch.setattr(vix_gate, "snapshot", lambda: None)
    monkeypatch.setattr(config, "PREOPEN_WARM_BUDGET_S", 45.0)
    monkeypatch.setattr(config, "PREOPEN_WARM_STOP_AT_OPEN", False)
    monkeypatch.setattr(config, "PREOPEN_WARM_MAX_SYMBOLS", 40)
    n = preopen._warm_blocking([f"S{i}" for i in range(30)], _clock=clk)
    assert n == 3 and seen == ["S0", "S1", "S2"]


def test_warmup_budget_zero_is_unlimited(monkeypatch):
    from entry_engine import event_risk, prev_day, vix_gate
    clk = _Clock()
    monkeypatch.setattr(event_risk, "lookup", lambda s: setattr(clk, "t", clk.t + 20.0))
    monkeypatch.setattr(prev_day, "prefetch", lambda s: None)
    monkeypatch.setattr(vix_gate, "snapshot", lambda: None)
    monkeypatch.setattr(config, "PREOPEN_WARM_BUDGET_S", 0.0)
    monkeypatch.setattr(config, "PREOPEN_WARM_STOP_AT_OPEN", False)
    monkeypatch.setattr(config, "PREOPEN_WARM_MAX_SYMBOLS", 10)
    assert preopen._warm_blocking([f"S{i}" for i in range(10)], _clock=clk) == 10


def test_warmup_stops_once_market_is_open(monkeypatch):
    from entry_engine import event_risk, prev_day, vix_gate
    seen = []
    monkeypatch.setattr(event_risk, "lookup", lambda s: seen.append(s))
    monkeypatch.setattr(prev_day, "prefetch", lambda s: None)
    monkeypatch.setattr(vix_gate, "snapshot", lambda: None)
    monkeypatch.setattr(config, "PREOPEN_WARM_STOP_AT_OPEN", True)
    monkeypatch.setattr(preopen, "ist_time_at_or_after", lambda t: True)
    assert preopen._warm_blocking(["A", "B"]) == 0 and seen == []
    monkeypatch.setattr(preopen, "ist_time_at_or_after", lambda t: False)
    assert preopen._warm_blocking(["A", "B"]) == 2


# ------------------------------------------------------------------ #4 held coverage
def test_weak_symbols_picks_missing_feed_and_delisted():
    rows = [{"symbol": "GOOD", "primary_feed": True}, {"symbol": "BIRET", "primary_feed": False},
            {"symbol": "OLD", "primary_feed": True, "known_delisted": True}, {"symbol": "UNK", "primary_feed": None}]
    assert [r["symbol"] for r in held_coverage.weak_symbols(rows)] == ["BIRET", "OLD"]


class _Resp:
    def __init__(self, rows, code=200):
        self.status_code, self._rows = code, rows

    def json(self):
        return {"symbols": self._rows}


class _Client:
    def __init__(self, rows, code=200):
        self.rows, self.code, self.posts = rows, code, []

    async def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        return _Resp(self.rows, self.code)

    async def aclose(self):
        pass


@pytest.fixture()
def hc(monkeypatch):
    held_coverage._last_run[0] = -1e12
    held_coverage._alerted.clear()
    monkeypatch.setenv("HELD_COVERAGE_CHECK_INTERVAL_S", "600")
    import notifier
    sent = []
    monkeypatch.setattr(notifier, "notify_fire_and_forget", lambda t: sent.append(t))
    return sent


def test_check_reports_weak_held_symbol_and_alerts_once_per_day(hc):
    c = _Client([{"symbol": "BIRET", "primary_feed": False}, {"symbol": "HEG", "primary_feed": True}])
    weak = run(held_coverage.check(["biret.ns", "HEG"], now_mono=1000.0, client=c))
    assert [w["symbol"] for w in weak] == ["BIRET"] and len(hc) == 1
    assert c.posts[0][1] == {"symbols": ["BIRET", "HEG"]}
    run(held_coverage.check(["BIRET"], now_mono=2000.0, client=c))      # interval passed: checked again, no second alert
    assert len(hc) == 1 and len(c.posts) == 2


def test_check_is_throttled_and_can_be_switched_off(hc, monkeypatch):
    c = _Client([])
    run(held_coverage.check(["A"], now_mono=1000.0, client=c))
    run(held_coverage.check(["A"], now_mono=1100.0, client=c))
    assert len(c.posts) == 1
    held_coverage._last_run[0] = -1e12
    monkeypatch.setenv("HELD_COVERAGE_CHECK_INTERVAL_S", "0")
    assert run(held_coverage.check(["A"], now_mono=9000.0, client=c)) == [] and len(c.posts) == 1


def test_check_never_raises_and_ignores_http_errors(hc):
    assert run(held_coverage.check(["A"], now_mono=1000.0, client=_Client([], code=500))) == []
    assert run(held_coverage.check([], now_mono=2000.0, client=_Client([]))) == []


# ------------------------------------------------------------------ #1 priority flag
class _FlagClient:
    def __init__(self):
        self.bodies = []

    async def post(self, url, json=None, timeout=None):
        self.bodies.append(json)

        class R:
            status_code = 500
        return R()


def test_bulk_ticks_sends_priority_flag_only_when_asked():
    c = _FlagClient()
    run(feed._bulk_ticks(c, ["AAA"], timeout=1.0))
    run(feed._bulk_ticks(c, ["AAA"], timeout=1.0, priority=True))
    assert c.bodies[0] == {"symbols": ["AAA"]}
    assert c.bodies[1] == {"symbols": ["AAA"], "priority": True}


# ------------------------------------------------------------------ #5 end to end: fetch_watchlist_candidates while Hot Picks warms
def _fetch_with(client, snapshot, saved):
    from unittest.mock import AsyncMock, patch
    from test_group177_watchlist_tier1_reasons import _breaker
    with patch("httpx.AsyncClient", return_value=client), \
         patch.object(sources, "api_gateway_breaker", _breaker()), \
         patch.object(sources, "event_service_breaker", _breaker()), \
         patch.object(sources, "save_snapshot", side_effect=lambda db, k, p: saved.append((k, p))), \
         patch.object(sources, "load_snapshot", return_value=snapshot), \
         patch.object(sources, "_tier3_volume_shock", new=AsyncMock(return_value=[])):
        return run(sources.fetch_watchlist_candidates(object(), "DEMO"))


def _warm_env(monkeypatch):
    monkeypatch.setenv("WATCHLIST_TIER1_WARMUP_WAIT_S", "0")
    monkeypatch.setenv("WATCHLIST_TIER1_WARMUP_RETRIES", "2")
    monkeypatch.setenv("WATCHLIST_TIER1_WARMUP_MIN_GAP_S", "120")
    sources._warm_wait_last[0] = -1e9
    sources._warm_wait_fails[0] = 0


def test_warming_uses_todays_snapshot_without_waiting_or_overwriting_it(monkeypatch):
    from test_group226_tier1_hot_picks_warmup import _SeqClient, WARMING
    from test_group177_watchlist_tier1_reasons import HOT
    _warm_env(monkeypatch)
    snap = sources._stamp_snapshot({"hot_picks": HOT, "ipo": {}})
    saved = []
    c = _SeqClient([WARMING])
    out = _fetch_with(c, snap, saved)
    assert [x["symbol"] for x in out] == ["TCS"]
    assert c.hot_calls == 1                      # no re-polls
    assert saved == []                           # the warming answer / replayed snapshot is not saved over the good one


def test_warming_without_a_snapshot_waits_then_backs_off(monkeypatch):
    from test_group226_tier1_hot_picks_warmup import _SeqClient, WARMING
    _warm_env(monkeypatch)
    saved = []
    c = _SeqClient([WARMING])
    _fetch_with(c, None, saved)
    assert c.hot_calls == 3 and sources._warm_wait_fails[0] == 1
    assert saved == []                           # an empty warming answer is never saved
    sources._warm_wait_last[0] = __import__("time").monotonic() - 150.0     # past the old 120 s gap, inside the doubled 240 s gap
    c2 = _SeqClient([WARMING])
    _fetch_with(c2, None, saved)
    assert c2.hot_calls == 1                     # still backing off: no re-poll


def test_a_real_answer_is_saved_with_a_stamp_and_resets_the_backoff(monkeypatch):
    from test_group226_tier1_hot_picks_warmup import _SeqClient
    from test_group177_watchlist_tier1_reasons import HOT
    _warm_env(monkeypatch)
    sources._warm_wait_fails[0] = 3
    saved = []
    _fetch_with(_SeqClient([HOT]), None, saved)
    assert saved and saved[0][0] == "tier1_hot_picks" and sources._SNAP_STAMP in saved[0][1]
