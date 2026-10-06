"""
tests/test_group211_angelone_budget.py

group211 (2026-10-06, item 1 of the open list): ONE shared AngelOne budget -- priority lanes
(POSITION > CANDIDATE > BACKGROUND), a token reserve each lane keeps free for the lanes above it, and a
single global cooldown that every AngelOne caller honours after a rate-limit answer from ANY endpoint.

No network, no DB, no AngelOne credentials.

Run from services/market-data-service:
    python -m pytest tests/test_group211_angelone_budget.py -v
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import types
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pytest

import angelone_budget as b
import angelone_client as ac
import rate_limiter as rl


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("ANGELONE_BUDGET", "ANGELONE_GLOBAL_COOLDOWN_S", "ANGELONE_GLOBAL_COOLDOWN_MAX_S",
              "ANGELONE_LANE_RESERVE_CANDIDATE", "ANGELONE_LANE_RESERVE_BACKGROUND",
              "ANGELONE_POSITION_LANE_REFRESH_S", "ANGELONE_HOT_DEMAND_WINDOW_S"):
        monkeypatch.delenv(k, raising=False)
    b._reset()
    rl._buckets.clear()
    yield
    b._reset()
    rl._buckets.clear()


# ── env helpers ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("val,expected", [("", True), ("  ", True), ("1", True), ("yes", True),
                                           ("0", False), ("false", False), ("OFF", False), (" no ", False)])
def test_enabled_is_blank_safe(monkeypatch, val, expected):
    monkeypatch.setenv("ANGELONE_BUDGET", val)
    assert b.enabled() is expected


def test_env_float_rejects_garbage_nan_and_out_of_range(monkeypatch):
    monkeypatch.setenv("X_F", "abc")
    assert b._env_float("X_F", 7.0) == 7.0
    monkeypatch.setenv("X_F", "nan")
    assert b._env_float("X_F", 7.0) == 7.0
    monkeypatch.setenv("X_F", "-1")
    assert b._env_float("X_F", 7.0, lo=0.0) == 7.0
    monkeypatch.setenv("X_F", "99")
    assert b._env_float("X_F", 7.0, hi=10.0) == 7.0
    monkeypatch.setenv("X_F", " 3.5 ")
    assert b._env_float("X_F", 7.0, 0.0, 10.0) == 3.5


def test_reserve_fractions_defaults_and_overrides(monkeypatch):
    assert b.reserve_fraction(b.POSITION) == 0.0
    assert b.reserve_fraction(None) == 0.0
    assert b.reserve_fraction(b.CANDIDATE) == 0.25
    assert b.reserve_fraction(b.BACKGROUND) == 0.5
    monkeypatch.setenv("ANGELONE_LANE_RESERVE_BACKGROUND", "0.8")
    assert b.reserve_fraction(b.BACKGROUND) == 0.8
    monkeypatch.setenv("ANGELONE_LANE_RESERVE_BACKGROUND", "5")      # above the 0.9 cap -> default
    assert b.reserve_fraction(b.BACKGROUND) == 0.5


def test_cooldown_max_never_below_base(monkeypatch):
    monkeypatch.setenv("ANGELONE_GLOBAL_COOLDOWN_S", "100")
    monkeypatch.setenv("ANGELONE_GLOBAL_COOLDOWN_MAX_S", "10")
    assert b._cooldown_max_s() == 100.0


# ── global cooldown ──────────────────────────────────────────────────────────
def test_trip_starts_one_cooldown_every_caller_sees(caplog):
    assert not b.in_global_cooldown() and b.cooldown_remaining() == 0.0
    with caplog.at_level(logging.WARNING, logger="angelone-budget"):
        assert b.trip("getCandleData") == 30.0
    assert b.in_global_cooldown()
    assert 29.0 < b.cooldown_remaining() <= 30.0
    assert [r for r in caplog.records if "getCandleData" in r.getMessage()]
    # every lane, and unclassified callers, skip -- and are counted
    for lane in (b.POSITION, b.CANDIDATE, b.BACKGROUND, None, "weird"):
        assert b.skip(lane) is True
    c = b.stats()["lanes"]
    assert c["position"]["skipped_cooldown"] == 1 and c["unclassified"]["skipped_cooldown"] == 2


def test_late_403s_during_a_running_cooldown_do_not_escalate_it():
    assert b.trip("quote") == 30.0
    assert b.trip("quote(batch)") == 0.0
    assert b.trip("getCandleData") == 0.0
    s = b.stats()
    assert s["trips"] == 1 and s["suppressed_late_403s"] == 2 and s["last_trip_endpoint"] == "quote"


def test_second_trip_within_window_doubles_up_to_the_cap(monkeypatch):
    assert b.trip("a") == 30.0
    b._cool_until = time.time() - 1          # first cooldown over
    assert b.trip("b") == 60.0               # doubled
    b._cool_until = time.time() - 1
    assert b.trip("c") == 60.0               # capped at ANGELONE_GLOBAL_COOLDOWN_MAX_S


def test_trip_after_the_escalation_window_starts_from_base_again():
    assert b.trip("a") == 30.0
    b._cool_until = time.time() - 1
    b._last_trip_at = time.time() - (b._ESCALATE_WINDOW_S + 5)
    assert b.trip("b") == 30.0


def test_cooldown_expires():
    b.trip("a")
    b._cool_until = time.time() - 0.01
    assert not b.in_global_cooldown() and b.skip(b.CANDIDATE) is False


def test_budget_off_means_no_cooldown_and_no_skip(monkeypatch):
    monkeypatch.setenv("ANGELONE_BUDGET", "0")
    assert b.trip("x") == 0.0
    assert not b.in_global_cooldown() and b.cooldown_remaining() == 0.0
    assert b.skip(b.CANDIDATE) is False
    assert b.lane_for("TCS") is None and b.lane_for_symbols(["TCS"]) is None
    assert b.position_symbols() == frozenset()


# ── lane admission ───────────────────────────────────────────────────────────
def test_position_and_unclassified_are_admitted_at_once_even_with_an_empty_bucket():
    bucket = rl._get_bucket("angelone_quote")
    bucket.tokens = 0.0
    bucket.updated = time.time()
    assert run(b.admit(b.POSITION, "angelone_quote", 1, 0.0)) is True
    assert run(b.admit(None, "angelone_quote", 1, 0.0)) is True
    c = b.stats()["lanes"]
    assert c["position"]["admitted"] == 1 and c["unclassified"]["admitted"] == 1


def test_background_is_shed_when_the_bucket_is_at_or_below_its_reserve():
    # quote bucket: burst 8 -> BACKGROUND needs 1 + 0.5*8 = 5 tokens, CANDIDATE needs 1 + 0.25*8 = 3
    bucket = rl._get_bucket("angelone_quote")
    bucket.rps = 0.0001                      # no refill during the test
    bucket.tokens = 4.0
    bucket.updated = time.time()
    assert run(b.admit(b.BACKGROUND, "angelone_quote", 1, 0.0)) is False
    assert run(b.admit(b.CANDIDATE, "angelone_quote", 1, 0.0)) is True
    bucket.tokens = 2.0
    assert run(b.admit(b.CANDIDATE, "angelone_quote", 1, 0.0)) is False
    assert run(b.admit(b.POSITION, "angelone_quote", 1, 0.0)) is True      # the reserve is for exactly this
    c = b.stats()["lanes"]
    assert c["background"]["shed"] == 1 and c["candidate"]["shed"] == 1 and c["candidate"]["admitted"] == 1


def test_background_waits_for_the_bucket_to_refill_then_is_admitted():
    bucket = rl._get_bucket("angelone_quote")
    bucket.rps = 50.0                        # refills well inside the wait
    bucket.tokens = 0.0
    bucket.updated = time.time()
    assert run(b.admit(b.BACKGROUND, "angelone_quote", 1, 2.0)) is True


def test_need_is_clamped_to_capacity_for_oversized_weight():
    bucket = rl._get_bucket("angelone_quote")
    bucket.tokens = bucket.capacity
    bucket.updated = time.time()
    assert run(b.admit(b.BACKGROUND, "angelone_quote", 50, 0.0)) is True   # "the whole bucket"


def test_zero_reserve_admits_at_once(monkeypatch):
    monkeypatch.setenv("ANGELONE_LANE_RESERVE_BACKGROUND", "0")
    bucket = rl._get_bucket("angelone_quote")
    bucket.tokens = 0.0
    bucket.updated = time.time()
    assert run(b.admit(b.BACKGROUND, "angelone_quote", 1, 0.0)) is True


def test_admit_returns_false_when_the_cooldown_starts_while_waiting():
    bucket = rl._get_bucket("angelone_quote")
    bucket.rps = 0.0001
    bucket.tokens = 0.0
    bucket.updated = time.time()

    async def go():
        t = asyncio.ensure_future(b.admit(b.BACKGROUND, "angelone_quote", 1, 5.0))
        await asyncio.sleep(0.1)
        b.trip("quote")
        return await t

    assert run(go()) is False
    assert b.stats()["lanes"]["background"]["skipped_cooldown"] == 1


def test_admit_fails_open_when_the_bucket_cannot_be_read(monkeypatch):
    monkeypatch.setattr(rl, "bucket_level", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    assert run(b.admit(b.BACKGROUND, "angelone_quote", 1, 0.0)) is True


def test_admit_fails_open_when_rate_limiter_cannot_be_imported(monkeypatch):
    monkeypatch.setitem(sys.modules, "rate_limiter", None)      # `import rate_limiter` -> ImportError
    assert run(b.admit(b.BACKGROUND, "angelone_quote", 1, 0.0)) is True


def test_bucket_level_peek_does_not_consume_and_refills():
    bucket = rl._get_bucket("angelone_quote")
    bucket.rps = 10.0
    bucket.tokens = 1.0
    bucket.updated = time.time() - 0.2
    tokens, cap = rl.bucket_level("angelone_quote")
    assert cap == 8.0 and 2.5 <= tokens <= 3.5
    assert bucket.tokens == 1.0                       # untouched
    bucket.updated = time.time() - 100
    assert rl.bucket_level("angelone_quote")[0] == cap   # never above capacity


# ── position lane / hot set ──────────────────────────────────────────────────
class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)


class _Conn:
    def __init__(self, tables):
        self.tables = tables

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt):
        sql = str(stmt)
        for name, rows in self.tables.items():
            if f"FROM {name} " in sql:
                if rows is None:
                    raise RuntimeError(f"no such table {name}")
                return _Rows([(r,) for r in rows])
        raise AssertionError(sql)


class _Engine:
    def __init__(self, tables):
        self.tables = tables

    def connect(self):
        return _Conn(self.tables)


def _stub_engine(monkeypatch, engine):
    import kv_cache
    monkeypatch.setattr(kv_cache, "_get_neon", lambda: engine)


def test_load_position_symbols_merges_both_tables_and_cleans_spelling(monkeypatch):
    _stub_engine(monkeypatch, _Engine({"trade_positions": ["hegam.ns", "TCS", None, ""],
                                       "scalp_positions": ["INFY", "TCS"]}))
    assert b._load_position_symbols() == {"HEGAM", "TCS", "INFY"}


def test_load_position_symbols_one_missing_table_contributes_nothing(monkeypatch):
    _stub_engine(monkeypatch, _Engine({"trade_positions": None, "scalp_positions": ["INFY"]}))
    assert b._load_position_symbols() == {"INFY"}


def test_load_position_symbols_none_when_every_query_fails_or_no_engine(monkeypatch):
    _stub_engine(monkeypatch, _Engine({"trade_positions": None, "scalp_positions": None}))
    assert b._load_position_symbols() is None
    _stub_engine(monkeypatch, None)
    assert b._load_position_symbols() is None


def test_load_position_symbols_none_when_engine_lookup_raises(monkeypatch):
    import kv_cache

    def boom():
        raise RuntimeError("no db")

    monkeypatch.setattr(kv_cache, "_get_neon", boom)
    assert b._load_position_symbols() is None


def _wait(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


def test_position_symbols_refreshes_in_the_background_and_never_blocks(monkeypatch):
    _stub_engine(monkeypatch, _Engine({"trade_positions": ["HEGAM"], "scalp_positions": []}))
    assert b.position_symbols() == frozenset()               # first call returns at once, refresh starts
    assert _wait(lambda: "HEGAM" in b._pos_symbols)
    assert b.position_symbols() == frozenset({"HEGAM"})
    assert b.stats()["position_symbols"] == 1 and b.stats()["position_symbols_age_s"] is not None


def test_failed_refresh_keeps_the_last_known_set_and_does_not_hammer_the_db(monkeypatch):
    b._pos_symbols = frozenset({"OLD"})
    calls = []
    monkeypatch.setattr(b, "_load_position_symbols", lambda: calls.append(1))   # -> None (unreachable)
    b.position_symbols()
    assert _wait(lambda: b._pos_loaded_at > 0 and not b._pos_refreshing)
    assert b.position_symbols() == frozenset({"OLD"}) and len(calls) == 1       # within the interval: no retry


def test_refresh_survives_an_unexpected_exception(monkeypatch):
    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(b, "_load_position_symbols", boom)
    b._pos_refreshing = True
    b._refresh_positions()
    assert b._pos_loaded_at > 0 and b._pos_refreshing is False


def test_refresh_interval_zero_turns_the_lookup_off(monkeypatch):
    monkeypatch.setenv("ANGELONE_POSITION_LANE_REFRESH_S", "0")
    b._pos_symbols = frozenset({"HEGAM"})
    assert b.position_symbols() == frozenset()
    assert b.lane_for("HEGAM", demand=False) == b.CANDIDATE


def test_thread_start_failure_resets_the_refreshing_flag(monkeypatch):
    class _T:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("cannot start thread")

    monkeypatch.setattr(b.threading, "Thread", _T)
    b.position_symbols()
    assert b._pos_refreshing is False


def test_lane_for_held_symbol_is_position_else_candidate_and_demand_is_optional():
    b._pos_symbols = frozenset({"HEGAM"})
    b._pos_loaded_at = time.time()
    assert b.lane_for("HEGAM.NS", demand=False) == b.POSITION
    assert b.lane_for("TCS", demand=False) == b.CANDIDATE
    assert b.hot_symbols() == []
    assert b.lane_for("TCS") == b.CANDIDATE                  # default demand=True -> hot
    assert b.hot_symbols() == ["TCS"]


def test_lane_for_symbols_is_position_if_any_is_held_and_can_record_demand():
    b._pos_symbols = frozenset({"HEGAM"})
    b._pos_loaded_at = time.time()
    assert b.lane_for_symbols(["TCS", "HEGAM"]) == b.POSITION
    assert b.lane_for_symbols(["TCS", "INFY"]) == b.CANDIDATE
    assert b.hot_symbols() == []                             # bulk lookups never flood the hot set
    assert b.lane_for_symbols(["TCS", "INFY"], demand=True) == b.CANDIDATE
    assert set(b.hot_symbols()) == {"TCS", "INFY"}


def test_hot_symbols_newest_first_windowed_limited_and_bounded(monkeypatch):
    now = time.time()
    b._demand.update({"OLD": now - 500, "A": now - 3, "B": now - 2, "C": now - 1})
    assert b.hot_symbols(2) == ["C", "B"]
    assert "OLD" not in b.hot_symbols(10)
    assert b.hot_symbols(0) == []
    b.note_demand("")                                        # blank: ignored
    assert "" not in b._demand
    # over the cap: expired entries are dropped on the next insert
    monkeypatch.setattr(b, "_DEMAND_MAX", 3)
    b.note_demand("D")
    assert "OLD" not in b._demand and "D" in b._demand


# ── stats ────────────────────────────────────────────────────────────────────
def test_stats_shape_and_bucket_filter():
    rl._get_bucket("angelone_quote")
    rl._get_bucket("yahoo")
    s = b.stats()
    assert s["enabled"] is True and s["global_cooldown_active"] is False
    assert set(s["lanes"]) == {"position", "candidate", "background", "unclassified"}
    assert "angelone_quote" in s["buckets"] and "yahoo" not in s["buckets"]


def test_stats_survives_rate_limiter_failure(monkeypatch):
    monkeypatch.setattr(rl, "stats", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert "buckets" not in b.stats()


# ── angelone_client wiring ───────────────────────────────────────────────────
class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = str(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class _Client:
    sent = []

    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def post(self, url, **kw):
        _Client.sent.append(url)
        return self.resp


def _session(monkeypatch, resp=None):
    s = ac.AngelOneSession()
    s.client_id, s.mpin, s.api_key, s.totp_secret = "C1", "1234", "KEY", "JBSWY3DPEHPK3PXP"
    s.token = "tok"
    s.token_expiry = datetime.utcnow() + timedelta(hours=1)
    monkeypatch.setattr(ac, "_resolve_client_public_ip", lambda: "1.2.3.4")
    monkeypatch.setattr(ac, "_rl_in_cooldown", lambda p: False)
    monkeypatch.setattr(ac, "_rl_acquire", lambda *a, **k: 0.0)
    monkeypatch.setattr(ac, "_rl_try_acquire", lambda *a, **k: True)
    _Client.sent = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client(resp or _Resp(200, {"data": {"fetched": []}})))
    return s


RATE_LIMITED = {"message": "Access denied because of exceeding access rate"}


@pytest.mark.parametrize("call", ["quote", "batch", "candles", "gainers"])
def test_every_endpoint_sends_nothing_while_the_global_cooldown_runs(monkeypatch, call):
    s = _session(monkeypatch)
    b.trip("elsewhere")
    if call == "quote":
        assert run(s.get_quote("NSE", "1", lane=b.POSITION)) == {}
    elif call == "batch":
        assert run(s.get_quotes_batch("NSE", ["1"], lane=b.POSITION)) == []
    elif call == "candles":
        assert run(s.get_candles("NSE", "1", "ONE_DAY", "a", "b", lane=b.POSITION)) == []
    else:
        assert run(s.get_gainers_losers()) == []
    assert _Client.sent == []


def test_a_403_on_candles_stops_quote_callers_too(monkeypatch):
    s = _session(monkeypatch, _Resp(403, RATE_LIMITED))
    assert run(s.get_candles("NSE", "1", "ONE_DAY", "a", "b")) == []
    assert b.in_global_cooldown() and b.stats()["last_trip_endpoint"] == "getCandleData"
    sent_before = len(_Client.sent)
    assert run(s.get_quote("NSE", "1")) == {}
    assert run(s.get_quotes_batch("NSE", ["1"])) == []
    assert len(_Client.sent) == sent_before                  # nothing more went out


def test_a_403_on_quote_and_on_batch_trip_the_global_cooldown(monkeypatch):
    s = _session(monkeypatch, _Resp(403, RATE_LIMITED))
    assert run(s.get_quote("NSE", "1")) == {}
    assert b.stats()["last_trip_endpoint"] == "quote"
    b._reset()
    assert run(s.get_quotes_batch("NSE", ["1"])) == []
    assert b.stats()["last_trip_endpoint"] == "quote(batch)"


def test_a_403_on_gainers_never_starts_the_global_cooldown(monkeypatch):
    s = _session(monkeypatch, _Resp(403, RATE_LIMITED))
    monkeypatch.setattr(ac, "_rl_set_cooldown", lambda *a, **k: None)
    assert run(s.get_gainers_losers()) == []
    assert not b.in_global_cooldown()


def test_background_lane_is_shed_without_sending_when_the_bucket_is_low(monkeypatch):
    s = _session(monkeypatch, _Resp(200, {"data": {"fetched": [{"symbolToken": "1"}]}}))
    bucket = rl._get_bucket("angelone_quote")
    bucket.rps = 0.0001
    bucket.tokens = 1.0
    bucket.updated = time.time()
    monkeypatch.setattr(ac, "_LANE_MAX_WAIT_S", 0.0)
    assert run(s.get_quotes_batch("NSE", ["1"], lane=b.BACKGROUND)) == []
    assert _Client.sent == []
    assert run(s.get_quotes_batch("NSE", ["1"], lane=b.POSITION)) == [{"symbolToken": "1"}]
    assert len(_Client.sent) == 1


def test_single_quote_candidate_lane_shed_but_position_goes_through(monkeypatch):
    s = _session(monkeypatch, _Resp(200, {"data": {"fetched": [{"ltp": 5}]}}))
    bucket = rl._get_bucket("angelone_quote")
    bucket.rps = 0.0001
    bucket.tokens = 1.0
    bucket.updated = time.time()
    assert run(s.get_quote("NSE", "1", max_wait=0.0, lane=b.CANDIDATE)) == {}
    assert _Client.sent == []
    assert run(s.get_quote("NSE", "1", max_wait=0.0, lane=b.POSITION)) == {"ltp": 5}


def test_candle_lane_shed_but_unclassified_keeps_old_behaviour(monkeypatch):
    s = _session(monkeypatch, _Resp(200, {"data": [[1, 2, 3, 4, 5, 6]]}))
    bucket = rl._get_bucket("angelone_candle")
    bucket.rps = 0.0001
    bucket.tokens = 1.0
    bucket.updated = time.time()
    monkeypatch.setattr(ac, "_CANDLE_MAX_WAIT_S", 0.0)
    assert run(s.get_candles("NSE", "1", "ONE_DAY", "a", "b", lane=b.BACKGROUND)) == []
    assert _Client.sent == []
    assert run(s.get_candles("NSE", "1", "ONE_DAY", "a", "b")) == [[1, 2, 3, 4, 5, 6]]


def test_client_helpers_never_raise_when_the_budget_module_is_missing_or_broken(monkeypatch):
    monkeypatch.setattr(ac, "_budget", None)
    assert ac._budget_skip(b.POSITION) is False
    assert run(ac._budget_admit(b.BACKGROUND, "angelone_quote", 1, 0.0)) is True
    ac._budget_trip("x")                                     # no-op

    boom = types.SimpleNamespace(
        skip=lambda lane: (_ for _ in ()).throw(RuntimeError("x")),
        admit=lambda *a: (_ for _ in ()).throw(RuntimeError("x")),
        trip=lambda ep: (_ for _ in ()).throw(RuntimeError("x")),
    )
    monkeypatch.setattr(ac, "_budget", boom)
    assert ac._budget_skip(b.POSITION) is False
    assert run(ac._budget_admit(b.BACKGROUND, "angelone_quote", 1, 0.0)) is True
    ac._budget_trip("x")                                     # swallowed


def test_budget_off_keeps_the_legacy_unclassified_path(monkeypatch):
    monkeypatch.setenv("ANGELONE_BUDGET", "0")
    s = _session(monkeypatch, _Resp(403, RATE_LIMITED))
    monkeypatch.setattr(ac, "_rl_set_cooldown", lambda *a, **k: None)
    assert run(s.get_quote("NSE", "1", lane=b.BACKGROUND)) == {}
    assert not b.in_global_cooldown()
    assert len(_Client.sent) == 1                            # sent: no lane gate, no global cooldown
    assert run(s.get_quote("NSE", "1", lane=b.BACKGROUND)) == {}
    assert len(_Client.sent) == 2


# ── feed poll plan ───────────────────────────────────────────────────────────
@pytest.fixture()
def feed(monkeypatch):
    mh = types.ModuleType("market_hours")
    mh.is_feed_window_ist = lambda: True
    monkeypatch.setitem(sys.modules, "market_hours", mh)
    sys.modules.pop("angelone_ws_feed", None)
    import angelone_ws_feed as f
    monkeypatch.setattr(f, "BATCH_SIZE", 2)
    f._last_cold_poll = 0.0
    yield f
    sys.modules.pop("angelone_ws_feed", None)


def _tm(n):
    return {f"S{i}": str(i + 1) for i in range(n)}


def test_plan_puts_held_first_then_hot_then_cold_in_lane_batches(feed):
    tm = _tm(7)
    b._pos_symbols = frozenset({"S5", "S0"})
    b._pos_loaded_at = time.time()
    b.note_demand("S3")
    b.note_demand("S5")                                      # held AND hot -> stays in the position lane only
    b.note_demand("NOT_IN_UNIVERSE")
    plan = feed._plan_batches(list(tm.values()), tm, now=1000.0)
    assert plan[0] == (["1", "6"], b.POSITION)               # S0, S5 (sorted symbols)
    assert plan[1] == (["4"], b.CANDIDATE)                   # S3
    cold = [(t, l) for t, l in plan if l == b.BACKGROUND]
    assert sorted(x for t, _ in cold for x in t) == ["2", "3", "5", "7"]
    assert all(len(t) <= 2 for t, _ in plan)


def test_cold_part_is_refreshed_at_most_every_interval(feed):
    tm = _tm(5)
    toks = list(tm.values())
    first = feed._plan_batches(toks, tm, now=1000.0)
    assert any(l == b.BACKGROUND for _, l in first)
    second = feed._plan_batches(toks, tm, now=1010.0)        # 10 s later: no cold batches
    assert not any(l == b.BACKGROUND for _, l in second)
    third = feed._plan_batches(toks, tm, now=1031.0)         # 31 s after the first: cold again
    assert any(l == b.BACKGROUND for _, l in third)


def test_cold_interval_zero_restores_the_old_everything_every_cycle_plan(feed, monkeypatch):
    monkeypatch.setattr(feed, "FEED_COLD_INTERVAL_S", 0.0)
    tm = _tm(5)
    plan = feed._plan_batches(list(tm.values()), tm)
    assert plan == [(["1", "2"], None), (["3", "4"], None), (["5"], None)]


def test_budget_off_restores_the_old_plan(feed, monkeypatch):
    monkeypatch.setenv("ANGELONE_BUDGET", "0")
    tm = _tm(3)
    assert feed._plan_batches(list(tm.values()), tm) == [(["1", "2"], None), (["3"], None)]


def test_plan_failure_falls_back_to_the_old_plan(feed, monkeypatch):
    monkeypatch.setattr(b, "position_symbols", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    tm = _tm(3)
    assert feed._plan_batches(list(tm.values()), tm) == [(["1", "2"], None), (["3"], None)]


def test_hot_set_is_capped_by_feed_hot_max(feed, monkeypatch):
    monkeypatch.setattr(feed, "FEED_HOT_MAX", 1)
    tm = _tm(4)
    b.note_demand("S1")
    time.sleep(0.01)
    b.note_demand("S2")
    plan = feed._plan_batches(list(tm.values()), tm, now=1000.0)
    cand = [t for t, l in plan if l == b.CANDIDATE]
    assert cand == [["3"]]                                   # only the newest hot symbol (S2)


class _Session:
    def __init__(self):
        self.calls = []

    async def ensure_session(self):
        return None

    async def get_quotes_batch(self, exchange, tokens, lane=None):
        self.calls.append((list(tokens), lane))
        return [{"symbolToken": t, "ltp": 10.0, "open": 1, "high": 2, "low": 1, "close": 1, "tradeVolume": 5}
                for t in tokens]


def _one_cycle(feed, monkeypatch, n, *, cooldown=False, held=()):
    symbols = [f"S{i}" for i in range(n)]
    sess = _Session()
    acm = types.ModuleType("angelone_client")
    acm.get_session = lambda: sess
    sm = types.ModuleType("angelone_scrip_master")
    sm.get_tokens_bulk = lambda syms, wait_s=0: {x: str(i + 1) for i, x in enumerate(syms)}
    sm.status = lambda: {"loaded_symbols": 10}
    monkeypatch.setitem(sys.modules, "angelone_client", acm)
    monkeypatch.setitem(sys.modules, "angelone_scrip_master", sm)
    monkeypatch.setattr(feed, "_ensure_schema", lambda *a, **k: None)
    monkeypatch.setattr(feed, "BATCH_GAP_S", 0.0)
    monkeypatch.setattr(feed, "POLL_INTERVAL_S", 30.0)
    monkeypatch.setattr(feed, "DB_BATCH_WRITES", True)
    monkeypatch.setattr(feed, "_upsert_ticks_batch_sync", lambda rows: None)
    monkeypatch.setattr(feed, "_upsert_tick_sync", lambda *a: None)
    if held:
        b._pos_symbols = frozenset(held)
        b._pos_loaded_at = time.time()
    if cooldown:
        b.trip("quote")
    feed.start_feed_background(symbols)
    end = time.time() + 3
    while time.time() < end and feed._last_cycle_s is None and not cooldown:
        time.sleep(0.02)
    if cooldown:
        time.sleep(0.4)
    feed._running = False
    if feed._thread is not None:
        feed._thread.join(timeout=5)
    return sess


def test_poll_cycle_sends_lane_tagged_batches_held_first(feed, monkeypatch):
    sess = _one_cycle(feed, monkeypatch, 5, held=("S3",))
    assert sess.calls[0] == (["4"], b.POSITION)
    assert {l for _, l in sess.calls[1:]} == {b.BACKGROUND}
    assert sorted(t for ts, _ in sess.calls for t in ts) == ["1", "2", "3", "4", "5"]


def test_poll_cycle_sends_nothing_during_the_global_cooldown(feed, monkeypatch):
    sess = _one_cycle(feed, monkeypatch, 5, cooldown=True)
    assert sess.calls == []


def test_poll_failure_log_reports_the_symbol_range_of_the_failed_batch(feed, monkeypatch, caplog):
    class _Bad(_Session):
        async def get_quotes_batch(self, exchange, tokens, lane=None):
            raise RuntimeError("boom")

    bad = _Bad()
    acm = types.ModuleType("angelone_client")
    acm.get_session = lambda: bad
    sm = types.ModuleType("angelone_scrip_master")
    sm.get_tokens_bulk = lambda syms, wait_s=0: {x: str(i + 1) for i, x in enumerate(syms)}
    sm.status = lambda: {"loaded_symbols": 10}
    monkeypatch.setitem(sys.modules, "angelone_client", acm)
    monkeypatch.setitem(sys.modules, "angelone_scrip_master", sm)
    monkeypatch.setattr(feed, "_ensure_schema", lambda *a, **k: None)
    monkeypatch.setattr(feed, "BATCH_GAP_S", 0.0)
    monkeypatch.setattr(feed, "POLL_INTERVAL_S", 30.0)
    with caplog.at_level(logging.WARNING):
        feed.start_feed_background(["S0", "S1", "S2"])
        end = time.time() + 3
        while time.time() < end and not [r for r in caplog.records if "quote batch" in r.getMessage()]:
            time.sleep(0.02)
        feed._running = False
        feed._thread.join(timeout=5)
    msgs = [r.getMessage() for r in caplog.records if "quote batch" in r.getMessage()]
    assert any("(0-2) failed: boom" in m for m in msgs) and any("(2-3) failed: boom" in m for m in msgs)


# ── main.py wiring ───────────────────────────────────────────────────────────
def test_ao_lane_helper_classifies_and_never_raises(monkeypatch):
    import main as m
    b._pos_symbols = frozenset({"HEGAM"})
    b._pos_loaded_at = time.time()
    assert m._ao_lane("HEGAM", demand=True) == b.POSITION
    assert m._ao_lane("TCS") == b.CANDIDATE and "TCS" not in b.hot_symbols()     # demand off by default
    assert m._ao_lane("INFY", demand=True) == b.CANDIDATE and "INFY" in b.hot_symbols()
    assert m._ao_lane(["TCS", "HEGAM"]) == b.POSITION
    assert m._ao_lane(["TCS", ""]) == b.CANDIDATE
    monkeypatch.setenv("ANGELONE_BUDGET", "0")
    assert m._ao_lane("TCS") is None and m._ao_lane(["TCS"]) is None
    monkeypatch.setenv("ANGELONE_BUDGET", "1")
    monkeypatch.setattr(b, "lane_for", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert m._ao_lane("TCS") is None


def test_budget_status_route_reports_stats_and_degrades_gracefully(monkeypatch):
    from fastapi.testclient import TestClient
    import main as m
    c = TestClient(m.app, raise_server_exceptions=False)
    b.trip("getCandleData")
    r = c.get("/angelone/budget")
    assert r.status_code == 200
    j = r.json()
    assert j["enabled"] is True and j["global_cooldown_active"] is True
    assert j["last_trip_endpoint"] == "getCandleData" and "lanes" in j
    monkeypatch.setattr(b, "stats", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    r = c.get("/angelone/budget")
    assert r.status_code == 200 and r.json() == {"enabled": False, "error": "RuntimeError: boom"}
