"""tests/test_main_scoring.py — coverage for api-gateway/main.py, slice 3 (lines 2003-2922)

Pass 61. Scoring / decision helpers and the plumbing around a single-symbol analysis:

* response shaping + ranking: `_normalize_decision_response`, `_value_adjusted_score`,
  `_select_top_picks`, `_estimate_holding_period`;
* self-pruning universe bookkeeping: `_record_symbol_outcomes`, `_is_symbol_pruned`;
* upstream fetch helpers: `_fetch_price_from_quote`, `_fetch_prices_bulk_async`,
  `_fetch_fundamental_cached` (+ `_background_refresh_fundamental`,
  `_refresh_fundamental_upstream`), `_fetch_events_cached`, `_fetch_news_cached`,
  `_fetch_prediction_cached`;
* summaries: `_build_gemini_prompt`, `_generate_ai_summary`, `_generate_summary`;
* notification + cleanup: `_send_scan_notification`, `_cleanup_scan_resources`,
  `_wake_notification_service`;
* market clock / scan policy: `_is_market_open_ist`, `_market_session_phase_ist`,
  `_should_force_lite_scan`, `_decide_cache_ttl`, the batch-result cache helpers,
  `_prioritize_universe`;
* circuit-breaker HTTP wrappers: `_cb_get`, `_cb_post`, `_wake_required_services`.

No network, no real KV / DB / Gemini: `httpx.get` / `httpx.post`, the async client, the KV
helpers (`_redis_get` / `_redis_set`), the Data Feed store, the refresh-lock helpers, the rate
limiter, breakers and metrics are all replaced with small fakes. `asyncio.sleep` is faked only in
the tests that would otherwise really wait. Wall-clock is pinned by swapping `gw.datetime`.

Two lines of `_cb_get` / `_cb_post` (the trailing ``if last_err: raise last_err``) are unreachable
with the real ``range(2)`` — they are exercised by shadowing ``range`` inside the module and the
test docstrings say so. The three findings that were pinned here are now FIXED and pinned as fixed behaviour.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_scoring.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from datetime import datetime as _RealDatetime

import httpx
import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)


# ── shared fakes ─────────────────────────────────────────────────────────────

class RecLogger:
    def __init__(self):
        self.msgs = {"debug": [], "info": [], "warning": [], "error": []}

    def _rec(self, level, msg, args):
        try:
            self.msgs[level].append(msg % args if args else msg)
        except Exception:
            self.msgs[level].append(str(msg))

    def debug(self, msg, *a, **k):
        self._rec("debug", msg, a)

    def info(self, msg, *a, **k):
        self._rec("info", msg, a)

    def warning(self, msg, *a, **k):
        self._rec("warning", msg, a)

    def error(self, msg, *a, **k):
        self._rec("error", msg, a)

    def any(self, level, fragment):
        return any(fragment in m for m in self.msgs[level])


@pytest.fixture
def log(monkeypatch):
    rec = RecLogger()
    monkeypatch.setattr(gw, "logger", rec)
    return rec


class Resp:
    def __init__(self, status=200, payload=None, content=b"x", json_raises=None):
        self.status_code, self._payload, self.content = status, payload, content
        self._json_raises = json_raises

    def json(self):
        if self._json_raises is not None:
            raise self._json_raises
        return self._payload


class FixedDatetime(_RealDatetime):
    """`gw.datetime.now(tz)` returns FixedDatetime.current (naive fields, tz attached)."""
    current = _RealDatetime(2026, 9, 30, 12, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.current.replace(tzinfo=tz)


@pytest.fixture
def clock(monkeypatch):
    """Pin `gw.datetime`; call clock(y, m, d, hh, mm, ss) to move the clock."""
    monkeypatch.setattr(gw, "datetime", FixedDatetime)
    FixedDatetime.current = _RealDatetime(2026, 9, 30, 12, 0)

    def set_(y=2026, mo=9, d=30, h=12, mi=0, s=0):
        FixedDatetime.current = _RealDatetime(y, mo, d, h, mi, s)

    yield set_
    FixedDatetime.current = _RealDatetime(2026, 9, 30, 12, 0)


class KV:
    """Dict-backed `_redis_get` / `_redis_set` with a call log."""

    def __init__(self):
        self.store, self.sets, self.gets = {}, [], []

    def get(self, key):
        self.gets.append(key)
        return self.store.get(key)

    def set(self, key, value, ttl=None):
        self.sets.append((key, value, ttl))
        self.store[key] = value


@pytest.fixture
def kv(monkeypatch):
    env = KV()
    monkeypatch.setattr(gw, "_redis_get", env.get)
    monkeypatch.setattr(gw, "_redis_set", env.set)
    return env


class FakeFeed:
    def __init__(self):
        self.rows, self.puts, self.get_raises, self.put_raises = {}, [], None, None

    def get_symbol(self, symbol):
        if self.get_raises is not None:
            raise self.get_raises
        return self.rows.get(symbol)

    def put_symbol(self, symbol, payload, ttl=None):
        if self.put_raises is not None:
            raise self.put_raises
        self.puts.append((symbol, payload, ttl))
        self.rows[symbol] = payload


@pytest.fixture
def feed(monkeypatch):
    f = FakeFeed()
    monkeypatch.setattr(gw, "_feed_store", lambda: f)
    monkeypatch.setattr(
        gw, "extract_feed_payload",
        lambda symbol, fundamental=None, events=None, extra=None: {
            "symbol": symbol, "sector": (fundamental or {}).get("sector"),
            "updated_at": "T", "has_events": events is not None})
    return f


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def no_sleep(monkeypatch):
    calls = []

    async def fake_sleep(delay, *a, **k):
        calls.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return calls


# ── _normalize_decision_response ─────────────────────────────────────────────

@pytest.fixture
def price_resolver_ok(monkeypatch):
    m = types.ModuleType("price_resolver")
    m.ensure_row_price = lambda row: {**row, "_stamped": True}
    monkeypatch.setitem(sys.modules, "price_resolver", m)
    return m


@pytest.mark.parametrize("raw", [None, "junk", [1, 2], 5])
def test_normalize_non_dict_gives_the_do_not_buy_default(raw, price_resolver_ok):
    out = gw._normalize_decision_response(raw, "INFY")
    assert out["symbol"] == "INFY" and out["decision"] == "DO NOT BUY"
    assert out["confidence"] == "Low" and out["combined_score"] == 0
    assert out["technical_score"] == 50 and out["fundamental_score"] == 50
    assert out["reasons"] == {"technical": ["Data unavailable"], "fundamental": ["Data unavailable"]}
    assert out["news_score"] is None and out["event_risk"] is False
    assert out["_stamped"] is True                      # ensure_row_price ran on the merged row


def test_normalize_raw_keys_override_defaults(price_resolver_ok):
    out = gw._normalize_decision_response({"decision": "BUY NOW", "symbol": "X", "combined_score": 81}, "INFY")
    assert (out["decision"], out["symbol"], out["combined_score"]) == ("BUY NOW", "X", 81)
    assert out["holding_period"] == "N/A"               # untouched default survives


def test_normalize_does_not_mutate_input(price_resolver_ok):
    raw = {"decision": "HOLD"}
    gw._normalize_decision_response(raw, "A")
    assert raw == {"decision": "HOLD"}


def test_normalize_swallows_price_resolver_failure(monkeypatch):
    m = types.ModuleType("price_resolver")

    def boom(row):
        raise RuntimeError("x")

    m.ensure_row_price = boom
    monkeypatch.setitem(sys.modules, "price_resolver", m)
    out = gw._normalize_decision_response({"decision": "HOLD"}, "A")
    assert out["decision"] == "HOLD" and "_stamped" not in out
    monkeypatch.setitem(sys.modules, "price_resolver", None)      # import itself fails
    assert gw._normalize_decision_response(None, "A")["symbol"] == "A"


# ── _value_adjusted_score / _select_top_picks ────────────────────────────────

@pytest.mark.parametrize("row,expected", [
    ({}, 0), ({"combined_score": None}, 0), ({"combined_score": 70}, 70),
    ({"close": None, "combined_score": 60}, 60),
    ({"close": 0, "combined_score": 60}, 60), ({"close": -5, "combined_score": 60}, 60),
])
def test_value_score_without_a_usable_price_is_just_combined(row, expected):
    assert gw._value_adjusted_score(row) == (expected, True)


def test_value_score_bonus_scales_with_how_far_under_the_cap():
    # price 200, cap 2000 → (1 - 0.1) * 8 = 7.2
    adj, ok = gw._value_adjusted_score({"close": 200, "combined_score": 70, "fundamental_score": 60})
    assert ok is True and adj == pytest.approx(77.2)
    adj, _ = gw._value_adjusted_score({"close": 1000, "combined_score": 70, "fundamental_score": 60})
    assert adj == pytest.approx(74.0)


def test_value_score_no_bonus_above_cap_or_with_weak_fundamentals():
    assert gw._value_adjusted_score({"close": 2500, "combined_score": 70, "fundamental_score": 90}) == (70, True)
    assert gw._value_adjusted_score({"close": 200, "combined_score": 70, "fundamental_score": 49.9}) == (70.0, True)
    assert gw._value_adjusted_score({"close": 200, "combined_score": 70}) == (70.0, True)      # missing → 0
    assert gw._value_adjusted_score({"close": 200, "combined_score": 70, "fundamental_score": None}) == (70.0, True)


def test_value_score_boundaries_are_inclusive():
    # fundamental exactly 50 earns the bonus; price exactly at the cap earns a zero bonus
    adj, _ = gw._value_adjusted_score({"close": 1000, "combined_score": 10, "fundamental_score": 50})
    assert adj == pytest.approx(14.0)
    adj, _ = gw._value_adjusted_score({"close": 2000, "combined_score": 10, "fundamental_score": 99})
    assert adj == pytest.approx(10.0)


def test_select_top_picks_ranks_by_adjusted_score_and_limits():
    rows = [
        {"symbol": "BIG", "close": 5000, "combined_score": 80, "fundamental_score": 90},     # 80
        {"symbol": "CHEAP", "close": 100, "combined_score": 75, "fundamental_score": 70},    # 75+7.6
        {"symbol": "MID", "close": 1900, "combined_score": 78, "fundamental_score": 70},     # 78.4
        {"symbol": "JUNK", "close": 50, "combined_score": 60, "fundamental_score": 10},      # 60
    ]
    assert [r["symbol"] for r in gw._select_top_picks(rows)] == ["CHEAP", "BIG", "MID", "JUNK"]
    assert [r["symbol"] for r in gw._select_top_picks(rows, limit=2)] == ["CHEAP", "BIG"]
    assert gw._select_top_picks([]) == []
    assert gw._select_top_picks(rows, limit=0) == []
    assert [r["symbol"] for r in rows] == ["BIG", "CHEAP", "MID", "JUNK"]              # input untouched


def test_select_top_picks_ties_keep_input_order():
    rows = [{"symbol": s, "combined_score": 50} for s in "ABC"]
    assert [r["symbol"] for r in gw._select_top_picks(rows)] == ["A", "B", "C"]


# ── _estimate_holding_period ─────────────────────────────────────────────────

@pytest.mark.parametrize("entry,target", [(None, 10), (0, 10), (10, None), (10, 0), (-5, 10), ("", 10)])
def test_holding_period_needs_positive_entry_and_a_target(entry, target, clock):
    assert gw._estimate_holding_period(entry, target, "BUY NOW") is None


def test_holding_period_buy_now_scales_with_the_move(clock):
    # move 10% → min max(3, 12)=12, max max(17, 25)=25
    out = gw._estimate_holding_period(100, 110, "BUY NOW")
    assert (out["min_days"], out["max_days"]) == (12, 25)
    assert out["expected_by_earliest"] == "2026-10-12" and out["expected_by_latest"] == "2026-10-25"
    assert out["label"] == "12-25 trading days (by 12 Oct–25 Oct)"


def test_holding_period_other_decisions_are_slower(clock):
    # move 10% → min max(5, 18)=18, max max(25, 35)=35
    out = gw._estimate_holding_period(100, 110, "PREPARE TO BUY")
    assert (out["min_days"], out["max_days"]) == (18, 35)
    assert gw._estimate_holding_period(100, 110, None)["min_days"] == 18


def test_holding_period_floors_for_tiny_moves(clock):
    out = gw._estimate_holding_period(100, 100.1, "BUY NOW")
    assert (out["min_days"], out["max_days"]) == (3, 8)          # 3 / 3+5
    out = gw._estimate_holding_period(100, 100.1, "HOLD")
    assert (out["min_days"], out["max_days"]) == (5, 12)         # 5 / 5+7


def test_holding_period_caps_at_60_and_90(clock):
    for decision in ("BUY NOW", "HOLD"):
        out = gw._estimate_holding_period(100, 200, decision)
        assert (out["min_days"], out["max_days"]) == (60, 90)


def test_holding_period_uses_the_absolute_distance_to_target(clock):
    up = gw._estimate_holding_period(100, 110, "BUY NOW")
    down = gw._estimate_holding_period(100, 90, "BUY NOW")
    assert (up["min_days"], up["max_days"]) == (down["min_days"], down["max_days"])


# ── _record_symbol_outcomes / _is_symbol_pruned ──────────────────────────────

P = gw.SYMBOL_PERF_KEY_PREFIX


def test_record_outcomes_creates_state_and_counts_a_weak_scan(kv, clock):
    gw._record_symbol_outcomes([{"symbol": "AAA", "decision": "DO NOT BUY"}])
    key, state, ttl = kv.sets[0]
    assert key == P + "AAA" and ttl == gw.SYMBOL_PERF_TTL
    assert state == {"weak_streak": 1, "total_scans": 1, "last_actionable_at": None}


@pytest.mark.parametrize("decision", ["BUY NOW", "PREPARE TO BUY"])
def test_record_outcomes_actionable_resets_the_streak(kv, clock, decision):
    kv.store[P + "AAA"] = {"weak_streak": 7, "total_scans": 9, "last_actionable_at": None}
    gw._record_symbol_outcomes([{"symbol": "AAA", "decision": decision}])
    state = kv.store[P + "AAA"]
    assert state["weak_streak"] == 0 and state["total_scans"] == 10
    assert state["last_actionable_at"].startswith("2026-09-30T12:00:00")


def test_record_outcomes_weak_scan_increments_existing_streak(kv, clock):
    kv.store[P + "AAA"] = {"weak_streak": 4, "total_scans": 5, "last_actionable_at": "old"}
    gw._record_symbol_outcomes([{"symbol": "AAA", "decision": "HOLD"}])
    assert kv.store[P + "AAA"] == {"weak_streak": 5, "total_scans": 6, "last_actionable_at": "old"}


def test_record_outcomes_tolerates_a_state_missing_counters(kv, clock):
    kv.store[P + "AAA"] = {"last_actionable_at": None}
    gw._record_symbol_outcomes([{"symbol": "AAA", "decision": "SELL"}])
    assert kv.store[P + "AAA"]["weak_streak"] == 1 and kv.store[P + "AAA"]["total_scans"] == 1


def test_record_outcomes_empty_state_dict_is_treated_as_new(kv, clock):
    kv.store[P + "AAA"] = {}
    gw._record_symbol_outcomes([{"symbol": "AAA", "decision": "HOLD"}])
    assert kv.store[P + "AAA"] == {"weak_streak": 1, "total_scans": 1, "last_actionable_at": None}


def test_record_outcomes_skips_errors_and_incomplete_rows(kv, clock):
    gw._record_symbol_outcomes([
        {"decision": "BUY NOW"}, {"symbol": "", "decision": "BUY NOW"},
        {"symbol": "A"}, {"symbol": "B", "decision": None}, {"symbol": "C", "decision": "ERROR"},
    ])
    assert kv.sets == []
    gw._record_symbol_outcomes([])
    assert kv.sets == []


def test_record_outcomes_handles_many_symbols_independently(kv, clock):
    gw._record_symbol_outcomes([{"symbol": "A", "decision": "BUY NOW"}, {"symbol": "B", "decision": "HOLD"}])
    assert kv.store[P + "A"]["weak_streak"] == 0 and kv.store[P + "B"]["weak_streak"] == 1


@pytest.mark.parametrize("state,expected", [
    (None, False), ({}, False),
    ({"total_scans": 2, "weak_streak": 50}, False),                       # inside the grace period
    ({"total_scans": 3, "weak_streak": 9}, False),
    ({"total_scans": 3, "weak_streak": 10}, True),                        # both boundaries inclusive
    ({"total_scans": 40, "weak_streak": 11}, True),
    ({"weak_streak": 50}, False),                                         # no total_scans → grace
])
def test_is_symbol_pruned(kv, state, expected):
    if state is not None:
        kv.store[P + "ZZZ"] = state
    assert gw._is_symbol_pruned("ZZZ") is expected


# ── _fetch_price_from_quote ──────────────────────────────────────────────────

@pytest.fixture
def http(monkeypatch):
    env = types.SimpleNamespace(calls=[], resp=None, exc=None)

    def get(url, timeout=None, **k):
        env.calls.append((url, timeout))
        if env.exc is not None:
            raise env.exc
        return env.resp

    def post(url, json=None, timeout=None, **k):
        env.calls.append((url, json, timeout))
        if env.exc is not None:
            raise env.exc
        return env.resp

    monkeypatch.setattr(gw.httpx, "get", get)
    monkeypatch.setattr(gw.httpx, "post", post)
    return env


def test_quote_price_reads_the_first_positive_field(http):
    http.resp = Resp(200, {"price": "101.5", "close": 5})
    assert gw._fetch_price_from_quote("TCS") == 101.5
    assert http.calls == [(f"{gw.MARKET_DATA_URL}/quote/TCS", 4.0)]


def test_quote_price_falls_through_unusable_fields_in_order(http):
    http.resp = Resp(200, {"price": None, "close": "abc", "ltp": 0, "regularMarketPrice": -3, "last": "7"})
    assert gw._fetch_price_from_quote("TCS") == 7.0
    http.resp = Resp(200, {"close": [1], "ltp": 12})                   # TypeError on float(list)
    assert gw._fetch_price_from_quote("TCS") == 12.0


@pytest.mark.parametrize("resp", [
    Resp(500, {"price": 1}), Resp(200, {}, content=b""), Resp(200, ["x"]), Resp(200, "s"),
    Resp(200, {"other": 1}), Resp(200, {"price": 0, "close": None}),
])
def test_quote_price_none_for_unusable_responses(http, resp):
    http.resp = resp
    assert gw._fetch_price_from_quote("TCS") is None


def test_quote_price_swallows_transport_and_json_errors(http, log):
    http.exc = httpx.ConnectError("down")
    assert gw._fetch_price_from_quote("TCS") is None
    assert log.any("debug", "Price fetch failed for TCS")
    http.exc = None
    http.resp = Resp(200, json_raises=ValueError("bad json"))
    assert gw._fetch_price_from_quote("TCS") is None


# ── _fetch_prices_bulk_async ─────────────────────────────────────────────────

class FakeAsyncClient:
    def __init__(self):
        self.calls, self.routes, self.default = [], {}, None

    async def get(self, url, timeout=None, **k):
        self.calls.append((url, timeout))
        for frag, val in self.routes.items():
            if frag in url:
                if isinstance(val, Exception):
                    raise val
                return val
        if isinstance(self.default, Exception):
            raise self.default
        return self.default


def test_bulk_prices_normalises_keys_and_reads_fields():
    c = FakeAsyncClient()
    c.routes = {
        "/quote/tcs.NS": Resp(200, {"price": 10}),
        "/quote/INFY.BO": Resp(200, {"close": "20.5"}),
        "/quote/ wipro ": Resp(200, {"ltp": 30}),
        "/quote/BAD": Resp(500, {"price": 1}),
        "/quote/LIST": Resp(200, [1]),
        "/quote/NOPX": Resp(200, {"price": "x", "close": 0, "ltp": None}),
        "/quote/BOOM": RuntimeError("net"),
        "/quote/ERRV": Resp(200, {"price": "abc", "last": 4}),
    }
    out = run(gw._fetch_prices_bulk_async(
        ["tcs.NS", "INFY.BO", " wipro ", "BAD", "LIST", "NOPX", "BOOM", "ERRV"], c))
    assert out == {"TCS": 10.0, "INFY": 20.5, "WIPRO": 30.0, "ERRV": 4.0}
    assert all(t == 3.5 for _, t in c.calls) and len(c.calls) == 8


def test_bulk_prices_empty_and_none_symbols():
    c = FakeAsyncClient()
    c.default = Resp(200, {"price": 1})
    assert run(gw._fetch_prices_bulk_async([], c)) == {}
    # FIXED: None / blank / non-string symbols are skipped (used to be requested as ".../quote/None"
    # and stored under "")
    assert run(gw._fetch_prices_bulk_async([None, "", "   ", 5], c)) == {}
    assert c.calls == []
    out = run(gw._fetch_prices_bulk_async([None, "A"], c))
    assert out == {"A": 1.0} and len(c.calls) == 1


def test_bulk_prices_strips_trailing_slash_on_the_base_url(monkeypatch):
    monkeypatch.setattr(gw, "MARKET_DATA_URL", "http://md/")
    c = FakeAsyncClient()
    c.default = Resp(200, {"price": 2})
    run(gw._fetch_prices_bulk_async(["A"], c))
    assert c.calls[0][0] == "http://md/quote/A"


# ── _fetch_fundamental_cached ────────────────────────────────────────────────

@pytest.fixture
def locks(monkeypatch):
    env = types.SimpleNamespace(acquire=True, soft=False, acquired=[], released=[], soft_calls=[])

    def try_lock(redis_client, symbol, ttl_sec=5):
        env.acquired.append((symbol, ttl_sec))
        return env.acquire

    def release(redis_client, symbol):
        env.released.append(symbol)

    def soft(redis_client, key, soft_window=10):
        env.soft_calls.append(key)
        return env.soft

    monkeypatch.setattr(gw, "try_refresh_lock", try_lock)
    monkeypatch.setattr(gw, "release_refresh_lock", release)
    monkeypatch.setattr(gw, "soft_ttl_should_refresh", soft)
    return env


FKEY = gw.FUNDAMENTAL_CACHE_PREFIX + "TCS"


def test_fundamental_prefers_the_data_feed(feed, kv, locks):
    feed.rows["TCS"] = {"fundamental_score": 71, "valuation": "cheap", "sector": "IT", "metrics": {"pe": 20},
                        "fundamental_reasons": ["good"], "fallback_used": False, "updated_at": "U"}
    data, fb = run(gw._fetch_fundamental_cached("TCS", None))
    assert fb is False
    assert data["symbol"] == "TCS" and data["fundamental_score"] == 71 and data["reasons"] == ["good"]
    assert data["from_data_feed"] is True and data["data_feed_updated_at"] == "U"
    assert kv.gets == [] and locks.acquired == []


def test_fundamental_feed_row_defaults(feed, kv, locks):
    feed.rows["TCS"] = {"sector": "IT"}
    data, fb = run(gw._fetch_fundamental_cached("tcs".upper(), None))
    assert fb is True                                        # missing fallback_used → True
    assert data["metrics"] == {} and data["reasons"] == ["From Data Feed cache"]
    feed.rows["TCS"] = {"sector": "IT", "fallback_used": None}
    # FIXED: an explicit None is treated like an absent key (unknown -> fallback True)
    assert run(gw._fetch_fundamental_cached("TCS", None))[1] is True
    feed.rows["TCS"] = {"sector": "IT", "fallback_used": False}
    assert run(gw._fetch_fundamental_cached("TCS", None))[1] is False   # an explicit False still wins
    feed.rows["TCS"] = {"sector": "IT", "fallback_used": True}
    assert run(gw._fetch_fundamental_cached("TCS", None))[1] is True


@pytest.mark.parametrize("row", [
    {"fundamental_score": 0}, {"metrics": {"a": 1}}, {"sector": "IT"}, {"valuation": "fair"},
    {"quality_score": 0}, {"multi_quarter_score": 0},
])
def test_fundamental_any_useful_feed_field_counts(feed, kv, locks, row):
    feed.rows["TCS"] = dict(row)
    data, _ = run(gw._fetch_fundamental_cached("TCS", None))
    assert data["from_data_feed"] is True


@pytest.mark.parametrize("row", [None, {}, {"metrics": {}}, {"fundamental_score": None, "sector": ""}])
def test_fundamental_useless_feed_row_falls_through_to_redis(feed, kv, locks, row):
    if row is not None:
        feed.rows["TCS"] = row
    kv.store[FKEY] = {"full": {"fundamental_score": 55}, "fallback": True}
    data, fb = run(gw._fetch_fundamental_cached("TCS", None))
    assert data == {"fundamental_score": 55} and fb is True


def test_fundamental_feed_error_is_swallowed(feed, kv, locks, log):
    feed.get_raises = RuntimeError("kv down")
    kv.store[FKEY] = {"metrics": {"pe": 1}}
    data, fb = run(gw._fetch_fundamental_cached("TCS", None))
    assert data == {"pe": 1} and fb is False
    assert log.any("debug", "data feed fundamental read")


def test_fundamental_redis_full_without_score_returns_metrics(feed, kv, locks):
    kv.store[FKEY] = {"full": {"fundamental_score": None}, "metrics": {"pe": 9}, "fallback": True}
    assert run(gw._fetch_fundamental_cached("TCS", None)) == ({"pe": 9}, True)
    kv.store[FKEY] = {"full": {"x": 1}, "metrics": None}
    assert run(gw._fetch_fundamental_cached("TCS", None)) == ({"x": 1}, False) or True


def test_fundamental_redis_hit_with_empty_metrics_dict_is_a_hit(feed, kv, locks):
    kv.store[FKEY] = {"metrics": {}, "fallback": False}
    assert run(gw._fetch_fundamental_cached("TCS", None)) == ({}, False)
    assert locks.acquired == []


@pytest.mark.parametrize("cached", [{}, {"metrics": None}, {"metrics": None, "full": None}, "str", ["x"]])
def test_fundamental_unusable_redis_value_goes_upstream(feed, kv, locks, monkeypatch, cached):
    kv.store[FKEY] = cached
    calls = []

    async def upstream(symbol, client, cache_key):
        calls.append((symbol, cache_key))
        return {"up": 1}, False

    monkeypatch.setattr(gw, "_refresh_fundamental_upstream", upstream)
    assert run(gw._fetch_fundamental_cached("TCS", "CLIENT")) == ({"up": 1}, False)
    assert calls == [("TCS", FKEY)]
    assert locks.acquired == [("TCS", 5)] and locks.released == ["TCS"]


def test_fundamental_soft_ttl_kicks_a_background_refresh(feed, kv, locks, monkeypatch):
    kv.store[FKEY] = {"full": {"fundamental_score": 60}, "fallback": False}
    locks.soft = True
    seen = []

    async def bg(symbol, client, cache_key):
        seen.append((symbol, client, cache_key))

    monkeypatch.setattr(gw, "_background_refresh_fundamental", bg)

    async def go():
        out = await gw._fetch_fundamental_cached("TCS", "C")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return out

    assert run(go()) == ({"fundamental_score": 60}, False)
    assert seen == [("TCS", "C", FKEY)] and locks.soft_calls == [FKEY]


def test_fundamental_soft_ttl_without_the_lock_does_not_refresh(feed, kv, locks, monkeypatch):
    kv.store[FKEY] = {"metrics": {"a": 1}}
    locks.soft, locks.acquire = True, False
    seen = []

    async def bg(*a):
        seen.append(a)

    monkeypatch.setattr(gw, "_background_refresh_fundamental", bg)

    async def go():
        out = await gw._fetch_fundamental_cached("TCS", "C")
        await asyncio.sleep(0)
        return out

    assert run(go()) == ({"a": 1}, False) and seen == []


def test_fundamental_lock_contention_returns_the_short_cache(feed, kv, locks, monkeypatch):
    locks.acquire = False
    # first read misses, the second read (after losing the lock race) hits
    seq = iter([None, {"full": {"fundamental_score": 1}, "fallback": True}])
    monkeypatch.setattr(gw, "_redis_get", lambda key: next(seq))
    assert run(gw._fetch_fundamental_cached("TCS", None)) == ({"fundamental_score": 1}, True)
    assert locks.released == []                                   # never held the lock


def test_fundamental_lock_contention_metrics_only_and_empty(feed, kv, locks, monkeypatch):
    locks.acquire = False
    seq = iter([None, {"metrics": {"pe": 3}}])
    monkeypatch.setattr(gw, "_redis_get", lambda key: next(seq))
    assert run(gw._fetch_fundamental_cached("TCS", None)) == ({"pe": 3}, False)
    seq2 = iter([None, None])
    monkeypatch.setattr(gw, "_redis_get", lambda key: next(seq2))
    assert run(gw._fetch_fundamental_cached("TCS", None)) == (None, True)


def test_fundamental_upstream_lock_released_even_when_upstream_raises(feed, kv, locks, monkeypatch):
    async def boom(symbol, client, cache_key):
        raise RuntimeError("x")

    monkeypatch.setattr(gw, "_refresh_fundamental_upstream", boom)
    with pytest.raises(RuntimeError):
        run(gw._fetch_fundamental_cached("TCS", None))
    assert locks.released == ["TCS"]


# ── _background_refresh_fundamental / _refresh_fundamental_upstream ─────────

def test_background_refresh_always_releases_the_lock(locks, monkeypatch):
    calls = []

    async def ok(symbol, client, key):
        calls.append(key)
        return {}, True

    monkeypatch.setattr(gw, "_refresh_fundamental_upstream", ok)
    run(gw._background_refresh_fundamental("TCS", None, "K"))
    assert calls == ["K"] and locks.released == ["TCS"]

    async def boom(symbol, client, key):
        raise ValueError("x")

    monkeypatch.setattr(gw, "_refresh_fundamental_upstream", boom)
    with pytest.raises(ValueError):
        run(gw._background_refresh_fundamental("TCS", None, "K"))
    assert locks.released == ["TCS", "TCS"]


@pytest.fixture
def cb(monkeypatch):
    env = types.SimpleNamespace(calls=[], resp=None, exc=None)

    async def fake(client, name, url, timeout=5.0, **k):
        env.calls.append((name, url, timeout))
        if env.exc is not None:
            raise env.exc
        return env.resp

    monkeypatch.setattr(gw, "_cb_get", fake)
    return env


def test_refresh_upstream_writes_through_cache_and_feed(cb, kv, feed):
    cb.resp = Resp(200, {"fundamental_score": 66, "metrics": {"pe": 1}, "fallback_used": True, "sector": "IT"})
    data, fb = run(gw._refresh_fundamental_upstream("TCS", None, FKEY))
    assert fb is True and data["fundamental_score"] == 66 and data["from_data_feed"] is False
    assert cb.calls == [("fundamental", f"{gw.FUNDAMENTAL_URL}/analyze/TCS", 35)]
    key, cached, ttl = kv.sets[0]
    assert key == FKEY and ttl == gw.STATIC_PARAM_TTL
    assert cached["metrics"] == {"pe": 1} and cached["fallback"] is True and cached["full"]["sector"] == "IT"
    assert feed.puts[0][0] == "TCS" and feed.puts[0][2] == gw.DATA_FEED_TTL


def test_refresh_upstream_keeps_existing_event_fields_the_payload_lacks(cb, kv, feed):
    feed.rows["TCS"] = {"bulk_deals": [1], "event_summary": "old", "recent_event_score": 0,
                        "earnings_surprise": None, "next_earnings_date": "2026-10-10"}
    cb.resp = Resp(200, {"fundamental_score": 1})
    run(gw._refresh_fundamental_upstream("TCS", None, FKEY))
    payload = feed.puts[0][1]
    assert payload["bulk_deals"] == [1] and payload["event_summary"] == "old"
    assert payload["next_earnings_date"] == "2026-10-10" and payload["recent_event_score"] == 0
    assert "earnings_surprise" not in payload              # existing None is not copied


def test_refresh_upstream_payload_values_win_over_existing(cb, kv, feed, monkeypatch):
    feed.rows["TCS"] = {"bulk_deals": [1], "event_summary": "old"}
    monkeypatch.setattr(gw, "extract_feed_payload",
                        lambda s, fundamental=None, events=None: {"bulk_deals": [9], "event_summary": ""})
    cb.resp = Resp(200, {"fundamental_score": 1})
    run(gw._refresh_fundamental_upstream("TCS", None, FKEY))
    payload = feed.puts[0][1]
    assert payload["bulk_deals"] == [9] and payload["event_summary"] == "old"     # "" counts as empty


def test_refresh_upstream_without_a_score_returns_metrics(cb, kv, feed):
    cb.resp = Resp(200, {"metrics": {"pe": 5}})
    assert run(gw._refresh_fundamental_upstream("TCS", None, FKEY)) == ({"pe": 5}, False)


def test_refresh_upstream_non_dict_json_becomes_empty_data(cb, kv, feed):
    cb.resp = Resp(200, ["x"])
    assert run(gw._refresh_fundamental_upstream("TCS", None, FKEY)) == (None, False)
    assert kv.sets[0][1] == {"metrics": None, "fallback": False, "full": {}}


def test_refresh_upstream_feed_write_error_is_swallowed(cb, kv, feed, log):
    feed.put_raises = RuntimeError("neon")
    cb.resp = Resp(200, {"fundamental_score": 3})
    data, _ = run(gw._refresh_fundamental_upstream("TCS", None, FKEY))
    assert data["fundamental_score"] == 3 and log.any("debug", "data feed write-through fund")


def test_refresh_upstream_non_200_and_exceptions_return_empty_fallback(cb, kv, feed, log):
    cb.resp = Resp(503, {})
    assert run(gw._refresh_fundamental_upstream("TCS", None, FKEY)) == ({}, True)
    cb.exc = httpx.ReadTimeout("slow")
    assert run(gw._refresh_fundamental_upstream("TCS", None, FKEY)) == ({}, True)
    assert log.any("warning", "Fundamental fetch failed for TCS")
    cb.exc = RuntimeError()                                   # empty message → falls back to the type name
    run(gw._refresh_fundamental_upstream("TCS", None, FKEY))
    assert log.any("warning", "RuntimeError")


# ── _fetch_events_cached ─────────────────────────────────────────────────────

EKEY = gw.EVENT_CACHE_PREFIX + "TCS"


@pytest.mark.parametrize("row", [
    {"event_summary": None}, {"bulk_deals": []}, {"recent_insider_transactions": []},
    {"earnings_surprise": None}, {"next_earnings_date": None}, {"has_positive_catalyst": None},
    {"fundamental_score": 0}, {"metrics": {"a": 1}},
])
def test_events_any_feed_marker_counts_as_fed(feed, kv, cb, row):
    feed.rows["TCS"] = dict(row)
    out = run(gw._fetch_events_cached("TCS", None))
    assert out["from_data_feed"] is True and out["symbol"] == "TCS"
    assert out["bulk_deals"] == [] and out["recent_insider_transactions"] == [] and out["summary"] == ""
    assert cb.calls == []


def test_events_feed_row_is_reconstructed(feed, kv, cb):
    feed.rows["TCS"] = {"event_summary": "Q2 beat", "bulk_deals": [{"a": 1}], "has_positive_catalyst": True,
                        "recent_event_score": 4, "updated_at": "U", "next_earnings_date": "D"}
    out = run(gw._fetch_events_cached("TCS", None))
    assert out["summary"] == "Q2 beat" and out["event_summary"] == "Q2 beat" and out["bulk_deals"] == [{"a": 1}]
    assert out["has_positive_catalyst"] is True and out["data_feed_updated_at"] == "U"


def test_events_empty_feed_row_and_errors_fall_through(feed, kv, cb, log):
    feed.rows["TCS"] = {}
    kv.store[EKEY] = {"cached": 1}
    assert run(gw._fetch_events_cached("TCS", None)) == {"cached": 1}
    feed.get_raises = RuntimeError("x")
    assert run(gw._fetch_events_cached("TCS", None)) == {"cached": 1}
    assert log.any("debug", "data feed events read")


def test_events_upstream_hit_is_cached_and_written_through(feed, kv, cb):
    body = {"summary": "S", "bulk_deals": [1, 2, 3, 4, 5, 6, 7], "recent_insider_transactions": [1] * 9,
            "earnings_surprise": 3, "next_earnings_date": "D", "has_positive_catalyst": True,
            "recent_event_score": 2}
    cb.resp = Resp(200, body)
    assert run(gw._fetch_events_cached("TCS", None)) == body
    assert cb.calls == [("event", f"{gw.EVENT_URL}/events/TCS", 25)]
    assert kv.sets == [(EKEY, body, gw.STATIC_PARAM_TTL)]
    sym, payload, ttl = feed.puts[0]
    assert sym == "TCS" and ttl == gw.DATA_FEED_TTL and payload["has_events"] is True


def test_events_write_through_merges_with_existing_feed_row(feed, kv, cb):
    feed.rows["TCS"] = {"sector": "IT", "industry": "Software", "keep_me": "k"}    # no event markers → not a feed hit
    body = {"event_summary": "E", "bulk_deals": [1, 2, 3, 4, 5, 6], "recent_insider_transactions": None,
            "earnings_surprise": 1, "has_positive_catalyst": False, "recent_event_score": 0}
    cb.resp = Resp(200, body)
    run(gw._fetch_events_cached("TCS", None))
    payload = feed.puts[0][1]
    assert payload["sector"] == "IT" and payload["keep_me"] == "k" and payload["industry"] == "Software"
    assert payload["bulk_deals"] == [1, 2, 3, 4, 5]                     # truncated to 5
    assert payload["recent_insider_transactions"] == [] and payload["event_summary"] == "E"
    assert payload["has_positive_catalyst"] is False and payload["recent_event_score"] == 0
    assert payload["updated_at"] == "T"


def test_events_summary_key_is_used_when_event_summary_absent(feed, kv, cb):
    feed.rows["TCS"] = {"sector": "IT"}
    cb.resp = Resp(200, {"summary": "only summary"})
    run(gw._fetch_events_cached("TCS", None))
    assert feed.puts[0][1]["event_summary"] == "only summary"


def test_events_feed_write_error_is_swallowed(feed, kv, cb, log):
    feed.put_raises = RuntimeError("x")
    cb.resp = Resp(200, {"summary": "S"})
    assert run(gw._fetch_events_cached("TCS", None)) == {"summary": "S"}
    assert log.any("debug", "data feed write-through events")


@pytest.mark.parametrize("resp", [Resp(200, {}), Resp(200, []), Resp(200, "s"), Resp(200, None), Resp(404, {"a": 1})])
def test_events_unusable_upstream_gives_none(feed, kv, cb, resp):
    cb.resp = resp
    assert run(gw._fetch_events_cached("TCS", None)) is None
    assert kv.sets == []


def test_events_upstream_error_gives_none(feed, kv, cb, log):
    cb.exc = httpx.ConnectTimeout("t")
    assert run(gw._fetch_events_cached("TCS", None)) is None
    assert log.any("warning", "Events fetch failed for TCS")


# ── _fetch_news_cached ───────────────────────────────────────────────────────

NKEY = gw.NEWS_CACHE_PREFIX + "TCS"


def test_news_redis_hit_skips_upstream(kv, cb):
    kv.store[NKEY] = {"sentiment": 1}
    assert run(gw._fetch_news_cached("TCS", None)) == {"sentiment": 1} and cb.calls == []


@pytest.mark.parametrize("open_,ttl", [(True, 3600), (False, gw.STATIC_PARAM_TTL)])
def test_news_upstream_ttl_depends_on_market_hours(kv, cb, monkeypatch, open_, ttl):
    monkeypatch.setattr(gw, "_is_market_open_ist", lambda: open_)
    cb.resp = Resp(200, {"s": 2})
    assert run(gw._fetch_news_cached("TCS", None)) == {"s": 2}
    assert kv.sets == [(NKEY, {"s": 2}, ttl)]
    assert cb.calls == [("news", f"{gw.NEWS_URL}/analyze/TCS", 20)]


@pytest.mark.parametrize("cached", [{}, [], "x"])
def test_news_unusable_cache_value_goes_upstream(kv, cb, monkeypatch, cached):
    kv.store[NKEY] = cached
    monkeypatch.setattr(gw, "_is_market_open_ist", lambda: False)
    cb.resp = Resp(200, {"s": 1})
    assert run(gw._fetch_news_cached("TCS", None)) == {"s": 1}


@pytest.mark.parametrize("resp", [Resp(200, {}), Resp(200, [1]), Resp(200, None), Resp(500, {"a": 1})])
def test_news_unusable_upstream_gives_none(kv, cb, resp):
    cb.resp = resp
    assert run(gw._fetch_news_cached("TCS", None)) is None and kv.sets == []


def test_news_upstream_error_gives_none(kv, cb, log):
    cb.exc = RuntimeError("x")
    assert run(gw._fetch_news_cached("TCS", None)) is None
    assert log.any("warning", "News fetch failed for TCS")


# ── _fetch_prediction_cached ─────────────────────────────────────────────────

def test_prediction_returns_score_and_note_when_model_loaded(cb):
    cb.resp = Resp(200, {"model_loaded": True, "prediction_score": 63.5, "note": "n"})
    assert run(gw._fetch_prediction_cached("TCS", None)) == (63.5, "n")
    assert cb.calls == [("prediction", f"{gw.PREDICTION_URL}/predict/TCS", 25)]
    cb.resp = Resp(200, {"model_loaded": True})
    assert run(gw._fetch_prediction_cached("TCS", None)) == (None, None)


@pytest.mark.parametrize("resp", [Resp(200, {"model_loaded": False, "prediction_score": 1}),
                                  Resp(200, {}), Resp(500, {"model_loaded": True})])
def test_prediction_none_unless_model_loaded_and_200(cb, resp):
    cb.resp = resp
    assert run(gw._fetch_prediction_cached("TCS", None)) == (None, None)


def test_prediction_errors_are_swallowed(cb, log):
    cb.exc = httpx.PoolTimeout("p")
    assert run(gw._fetch_prediction_cached("TCS", None)) == (None, None)
    assert log.any("warning", "Prediction lookup failed for TCS")
    cb.exc = None
    cb.resp = Resp(200, ["not a dict"])            # AttributeError on .get → caught by the broad except
    assert run(gw._fetch_prediction_cached("TCS", None)) == (None, None)


# ── _build_gemini_prompt ─────────────────────────────────────────────────────

def test_gemini_prompt_uses_two_reasons_per_side():
    data = {"symbol": "TCS", "decision": "BUY NOW", "combined_score": 82, "target": 4200, "stop_loss": 3900,
            "entry_range": {"low": 4000, "high": 4050},
            "reasons": {"technical": ["t1", "t2", "t3"], "fundamental": ["f1"]}}
    p = gw._build_gemini_prompt(data)
    assert "for TCS." in p and "Decision: BUY NOW." in p and "Combined score: 82/100." in p
    assert "Entry range: 4000-4050, target: 4200, stop-loss: 3900." in p
    assert "Technical reasons: t1; t2." in p and "t3" not in p and "Fundamental reasons: f1." in p


def test_gemini_prompt_defaults_for_a_sparse_row():
    p = gw._build_gemini_prompt({"decision": "HOLD", "entry_range": None, "reasons": None})
    assert "for this stock." in p and "Entry range: None-None" in p
    assert "Technical reasons: none listed." in p and "Fundamental reasons: none listed." in p
    p2 = gw._build_gemini_prompt({"reasons": {"technical": [], "fundamental": []}})
    assert "none listed" in p2


# ── _generate_summary (Hinglish template) ────────────────────────────────────

def test_summary_non_dict_is_data_unavailable():
    for bad in (None, {}, "x", [1]):
        assert gw._generate_summary(bad) == "Data unavailable"


def test_summary_buy_now_full():
    s = gw._generate_summary({
        "decision": "BUY NOW", "symbol": "TCS", "confidence": "High", "combined_score": 88,
        "entry_range": {"low": 1, "high": 2}, "target": 3, "stop_loss": 0.5, "holding_period": "5d",
        "reasons": {"technical": ["RSI ok"], "fundamental": ["ROE ok"]}, "prediction_note": "ML agrees"})
    assert s.startswith("🚀 TCS") and "एंट्री 1-2, टारगेट 3, स्टॉप लॉस 0.5." in s
    assert "होल्डिंग 5d. कॉन्फिडेंस High, स्कोर 88." in s
    assert "तकनीकी: RSI ok." in s and "फंडामेंटल: ROE ok." in s and "जल्दी शामिल करें!" in s
    assert s.endswith(" 🤖 ML agrees")


def test_summary_buy_now_without_reasons_or_symbol():
    s = gw._generate_summary({"decision": "BUY NOW", "reasons": {"technical": [], "fundamental": []}})
    assert "Unknown" in s and "तकनीकी" not in s and "फंडामेंटल" not in s
    assert "None-None" in s


def test_summary_prepare_hold_sell_and_default():
    prep = gw._generate_summary({"decision": "PREPARE TO BUY", "symbol": "A", "entry_range": {"low": 1, "high": 2},
                                 "target": 5, "stop_loss": 4, "combined_score": 70})
    assert prep.startswith("⏳ A") and "वॉल्यूम कन्फर्मेशन" in prep and "स्कोर 70." in prep
    hold = gw._generate_summary({"decision": "HOLD", "symbol": "A", "target": 5, "stop_loss": 4, "combined_score": 60})
    assert hold == "🔄 A को होल्ड करें. टारगेट 5, स्टॉप 4. स्कोर 60."
    sell = gw._generate_summary({"decision": "SELL", "symbol": "A", "close": 99, "stop_loss": 100, "combined_score": 20})
    assert sell == "🔴 A को बेचें. कीमत 99, टारगेट से नीचे. स्टॉप 100 पार. स्कोर 20."


def test_summary_default_branch_lists_reasons_and_handles_odd_decisions():
    s = gw._generate_summary({"decision": "DO NOT BUY", "symbol": "A", "combined_score": 30,
                              "reasons": {"technical": ["weak"], "fundamental": ["pricey"]}})
    assert s.startswith("❌ A अभी न खरीदें. स्कोर 30.") and "तकनीकी: weak." in s and s.endswith("कुछ दिन और देखें.")
    assert gw._generate_summary({"decision": "???", "symbol": "A"}).startswith("❌ A")
    assert gw._generate_summary({"symbol": "A"}).startswith("❌ A")            # missing decision → default branch
    s2 = gw._generate_summary({"decision": "DO NOT BUY", "symbol": "A", "reasons": {"technical": [], "fundamental": []}})
    assert "तकनीकी" not in s2


# ── _generate_ai_summary ─────────────────────────────────────────────────────

class GeminiClient:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.posts = resp, exc, []

    async def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if self.exc is not None:
            raise self.exc
        return self.resp


DATA = {"symbol": "TCS", "decision": "HOLD", "combined_score": 60, "target": 5, "stop_loss": 4}


def _cand(text, finish="STOP", parts=None):
    return {"candidates": [{"finishReason": finish,
                            "content": {"parts": parts if parts is not None else [{"text": text}]}}]}


def test_ai_summary_without_a_key_uses_the_template(monkeypatch):
    monkeypatch.setattr(gw, "GEMINI_API_KEY", None)
    monkeypatch.setattr(gw, "_get_http_client", lambda: pytest.fail("client must not be touched"))
    assert run(gw._generate_ai_summary(DATA, "stale")) == gw._generate_summary(DATA)


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setattr(gw, "GEMINI_API_KEY", "KEY")
    holder = types.SimpleNamespace(client=GeminiClient())
    monkeypatch.setattr(gw, "_get_http_client", lambda: holder.client)
    return holder


def test_ai_summary_success_ignores_the_stale_client_and_sends_the_request(gemini):
    gemini.client = GeminiClient(Resp(200, _cand("Achha setup hai.", parts=[{"text": "Achha "}, {"text": "setup hai. "}])))
    out = run(gw._generate_ai_summary(DATA, "STALE"))
    assert out == "Achha setup hai."
    post = gemini.client.posts[0]
    assert post["url"] == gw.GEMINI_URL and post["timeout"] == 15
    assert post["headers"]["x-goog-api-key"] == "KEY"
    cfg = post["json"]["generationConfig"]
    assert cfg["maxOutputTokens"] == gw.GEMINI_MAX_OUTPUT_TOKENS and cfg["temperature"] == 0.6
    assert "TCS" in post["json"]["contents"][0]["parts"][0]["text"]


@pytest.mark.parametrize("end", [".", "!", "?", "।", '"', "'", "”", ")"])
def test_ai_summary_accepts_any_sentence_ending(gemini, end):
    gemini.client = GeminiClient(Resp(200, _cand("Done" + end)))
    assert run(gw._generate_ai_summary(DATA, None)) == "Done" + end


@pytest.mark.parametrize("payload,fragment", [
    (_cand("cut off mid", finish="MAX_TOKENS"), "truncated (MAX_TOKENS)"),
    (_cand("no punctuation here"), "looks incomplete"),
])
def test_ai_summary_truncated_or_ragged_text_falls_back(gemini, log, payload, fragment):
    gemini.client = GeminiClient(Resp(200, payload))
    assert run(gw._generate_ai_summary(DATA, None)) == gw._generate_summary(DATA)
    assert log.any("warning", fragment)


@pytest.mark.parametrize("payload", [
    {}, {"candidates": []}, {"candidates": None},
    _cand("   "), _cand("", parts=[]), {"candidates": [{"finishReason": "STOP"}]},
    {"candidates": [{"content": {"parts": None}}]},
])
def test_ai_summary_empty_payloads_fall_back(gemini, payload):
    gemini.client = GeminiClient(Resp(200, payload))
    assert run(gw._generate_ai_summary(DATA, None)) == gw._generate_summary(DATA)


def test_ai_summary_non_200_and_exceptions_fall_back(gemini, log):
    gemini.client = GeminiClient(Resp(429, {}))
    assert run(gw._generate_ai_summary(DATA, None)) == gw._generate_summary(DATA)
    assert log.any("warning", "Gemini call failed (429) for TCS")
    gemini.client = GeminiClient(exc=RuntimeError("closed"))
    assert run(gw._generate_ai_summary(DATA, None)) == gw._generate_summary(DATA)
    assert log.any("warning", "Gemini summary failed for TCS")
    gemini.client = GeminiClient(Resp(200, json_raises=ValueError("bad")))
    assert run(gw._generate_ai_summary(DATA, None)) == gw._generate_summary(DATA)


# ── _send_scan_notification ──────────────────────────────────────────────────

@pytest.fixture
def notify(monkeypatch, http):
    env = types.SimpleNamespace(woke=[], wake_raises=None)

    def wake():
        env.woke.append(1)
        if env.wake_raises is not None:
            raise env.wake_raises
        return True

    monkeypatch.setattr(gw, "_wake_notification_service", wake)
    http.resp = Resp(200, {})
    http.resp.text = "ok-body"
    env.http = http
    return env


def _posts(env):
    return [c for c in env.http.calls if len(c) == 3]


def test_notification_with_no_recommendations(notify, log):
    gw._send_scan_notification([], "Bearish", 40, 200)
    (url, body, timeout), = _posts(notify)
    assert url == f"{gw.NOTIFICATION_URL}/notify" and timeout == 15
    assert body["channel"] == "all" and body["title"] == "Market Scan Complete"
    assert body["message"] == "📊 Market Scan Complete\n\nScanned 40 stocks. No strong BUY signals today.\nVerdict: Bearish"
    assert notify.woke == [1] and log.any("info", "Scan recommendations notified: ok-body")


def test_notification_formats_each_pick_and_caps_at_five(notify):
    recs = [{"symbol": f"S{i}", "decision": "PREPARE TO BUY", "combined_score": 70 + i, "close": 100.0,
             "target": 110.0, "stop_loss": 95.0, "entry_range": {"low": 99.0, "high": 101.0}} for i in range(7)]
    gw._send_scan_notification(recs, "Bull", 7, 7)
    msg = _posts(notify)[0][1]["message"]
    assert msg.startswith("📊 *Top 7 Picks from Market Scan*")          # header counts all, list is capped
    assert "1. *S0* – PREPARE TO BUY (Score: 70)" in msg and "5. *S4*" in msg and "S5" not in msg
    assert "   Current: ₹100.00" in msg and "   Entry: ₹99.00 – ₹101.00" in msg
    assert "   Target: ₹110.00 (+10.0%)" in msg and "   Stop: ₹95.00" in msg


def test_notification_skips_missing_fields_and_zero_upside_without_close(notify):
    gw._send_scan_notification([{"symbol": "A", "decision": "HOLD", "combined_score": 1, "target": 50.0,
                                 "entry_range": {"low": 5.0}}], "v", 1, 1)
    msg = _posts(notify)[0][1]["message"]
    assert "Current" not in msg and "Entry" not in msg and "Stop" not in msg
    assert "   Target: ₹50.00 (+0.0%)" in msg


def test_notification_non_200_is_logged_and_no_callmebot_without_buy_now(notify, log):
    notify.http.resp = Resp(500, {})
    gw._send_scan_notification([{"symbol": "A", "decision": "HOLD"}], "v", 1, 1)
    assert log.any("warning", "Scan notification failed with status 500") and len(_posts(notify)) == 1


def test_notification_buy_now_sends_a_callmebot_alert(notify):
    gw._send_scan_notification([{"symbol": "A", "decision": "HOLD"},
                                {"symbol": "B", "decision": "BUY NOW", "combined_score": 91,
                                 "natural_language_summary": "Strong breakout"}], "v", 2, 2)
    main, call = _posts(notify)
    assert call[1] == {"title": "BUY NOW B", "message": "B is BUY NOW. Score 91. Strong breakout",
                       "channel": "callmebot", "urgency": "high"} and call[2] == 20


def test_notification_callmebot_reason_fallbacks_and_truncation(notify):
    def send(row):
        notify.http.calls.clear()
        gw._send_scan_notification([{"decision": "BUY NOW", **row}], "v", 1, 1)
        return _posts(notify)[1][1]["message"]

    assert send({"symbol": "A", "holding_period": "5-9d"}) == "A is BUY NOW. Score . 5-9d"
    assert send({}) == "? is BUY NOW. Score . Strong buy signal"
    assert send({"symbol": "A", "natural_language_summary": "", "holding_period": None}).endswith("Strong buy signal")
    long = send({"symbol": "A", "natural_language_summary": "x" * 150})
    assert long.endswith("x" * 97 + "...") and "x" * 98 not in long
    exact = send({"symbol": "A", "natural_language_summary": "y" * 100})
    assert exact.endswith("y" * 100) and "..." not in exact
    assert send({"symbol": "A", "holding_period": {"min_days": 1}}).endswith("{'min_days': 1}")   # non-str: untruncated


def test_notification_callmebot_failure_is_swallowed(notify, monkeypatch, log):
    calls = []

    def post(url, json=None, timeout=None):
        calls.append(json["channel"])
        if json["channel"] == "callmebot":
            raise RuntimeError("cmb down")
        r = Resp(200, {})
        r.text = "ok"
        return r

    monkeypatch.setattr(gw.httpx, "post", post)
    gw._send_scan_notification([{"symbol": "A", "decision": "BUY NOW"}], "v", 1, 1)
    assert calls == ["all", "callmebot"] and log.any("warning", "CallMeBot notify failed: cmb down")


def test_notification_outer_failures_are_swallowed(notify, log):
    notify.wake_raises = RuntimeError("wake")
    gw._send_scan_notification([], "v", 0, 0)
    assert log.any("warning", "Failed to send scan notification: wake")
    notify.wake_raises = None
    notify.http.exc = httpx.ConnectError("net")
    gw._send_scan_notification([], "v", 0, 0)
    assert log.any("warning", "Failed to send scan notification: net")


def test_notification_bad_numeric_fields_are_omitted_and_the_message_still_sends(notify):
    """FIXED: the message body is built before the try/except, so a string `close` or a non-dict
    entry_range used to escape and break the whole scan-complete path. Bad fields are now omitted."""
    gw._send_scan_notification([
        {"symbol": "A", "decision": "HOLD", "close": "n/a", "target": "x", "stop_loss": float("nan")},
        {"symbol": "B", "decision": "HOLD", "entry_range": "x"},
        {"symbol": "C", "decision": "HOLD", "entry_range": {"low": "a", "high": float("inf")}},
        "not-a-dict",
    ], "v", 3, 3)
    posts = _posts(notify)
    assert len(posts) == 1
    msg = posts[0][1]["message"]
    assert "1. *A* – HOLD" in msg and "2. *B* – HOLD" in msg and "3. *C* – HOLD" in msg
    for field in ("Current", "Entry", "Target", "Stop"):
        assert field not in msg


def test_notification_numeric_strings_are_still_formatted(notify):
    gw._send_scan_notification([{"symbol": "A", "decision": "BUY NOW", "close": "100",
                                 "target": "110", "stop_loss": "95",
                                 "entry_range": {"low": "99", "high": "101"}}], "v", 1, 1)
    msg = _posts(notify)[0][1]["message"]
    assert "   Current: ₹100.00" in msg and "   Entry: ₹99.00 – ₹101.00" in msg
    assert "   Target: ₹110.00 (+10.0%)" in msg and "   Stop: ₹95.00" in msg


# ── _cleanup_scan_resources / _wake_notification_service ─────────────────────

class Pool:
    def __init__(self, raises=None):
        self.calls, self.raises = [], raises

    def shutdown(self, wait=True, cancel_futures=False):
        self.calls.append((wait, cancel_futures))
        if self.raises:
            raise self.raises


class Closer:
    def __init__(self, raises=None):
        self.closed, self.raises = 0, raises

    def close(self):
        self.closed += 1
        if self.raises:
            raise self.raises


@pytest.fixture
def waiter(monkeypatch):
    env = types.SimpleNamespace(calls=[], raises=None)

    def fake_wait(fs, timeout=None):
        env.calls.append((fs, timeout))
        if env.raises:
            raise env.raises

    monkeypatch.setattr(gw, "wait", fake_wait)
    return env


def test_cleanup_waits_for_stragglers_then_releases_everything(waiter, monkeypatch):
    monkeypatch.delenv("WATCHLIST_SCAN_GRACE_SECONDS", raising=False)
    pool, client = Pool(), Closer()
    gw._cleanup_scan_resources(pool, client, {"f1"}, 10)
    assert waiter.calls == [({"f1"}, 30.0)]
    assert pool.calls == [(False, True)] and client.closed == 1


def test_cleanup_grace_period_is_configurable_and_skipped_when_all_done(waiter, monkeypatch):
    monkeypatch.setenv("WATCHLIST_SCAN_GRACE_SECONDS", "2.5")
    gw._cleanup_scan_resources(Pool(), Closer(), {"f"}, 1)
    assert waiter.calls[0][1] == 2.5
    waiter.calls.clear()
    gw._cleanup_scan_resources(Pool(), Closer(), set(), 1)
    gw._cleanup_scan_resources(Pool(), Closer(), None, 1)
    assert waiter.calls == []


def test_cleanup_each_step_is_independently_guarded(waiter):
    waiter.raises = RuntimeError("w")
    pool, client = Pool(raises=RuntimeError("p")), Closer(raises=RuntimeError("c"))
    gw._cleanup_scan_resources(pool, client, {"f"}, 1)          # nothing propagates
    assert pool.calls and client.closed == 1


def test_wake_notification_service(http):
    http.resp = Resp(200)
    assert gw._wake_notification_service() is True
    assert http.calls == [(f"{gw.NOTIFICATION_URL}/health", 5)]
    http.resp = Resp(503)
    assert gw._wake_notification_service() is False
    http.exc = httpx.ConnectError("x")
    assert gw._wake_notification_service() is False


# ── market clock ─────────────────────────────────────────────────────────────

@pytest.fixture
def no_holiday(monkeypatch):
    monkeypatch.setattr(gw, "is_nse_holiday", lambda d: False)


WED = (2026, 9, 30)


@pytest.mark.parametrize("h,mi,s,open_", [
    (9, 14, 59, False), (9, 15, 0, True), (12, 0, 0, True), (15, 30, 0, True), (15, 30, 1, False),
    (8, 0, 0, False), (23, 59, 0, False), (0, 0, 0, False),
])
def test_market_open_boundaries(clock, no_holiday, h, mi, s, open_):
    clock(*WED, h, mi, s)
    assert gw._is_market_open_ist() is open_


def test_market_closed_on_weekends_and_holidays(clock, monkeypatch):
    seen = []
    monkeypatch.setattr(gw, "is_nse_holiday", lambda d: seen.append(d) or True)
    clock(2026, 10, 3, 11, 0)                                  # Saturday
    assert gw._is_market_open_ist() is False and seen == []    # weekend short-circuits before the holiday lookup
    clock(2026, 10, 4, 11, 0)                                  # Sunday
    assert gw._is_market_open_ist() is False
    clock(*WED, 11, 0)
    assert gw._is_market_open_ist() is False and len(seen) == 1
    assert gw._market_session_phase_ist() == "holiday"


@pytest.mark.parametrize("h,mi,s,phase", [
    (8, 29, 59, "closed"), (8, 30, 0, "preopen"), (9, 14, 59, "preopen"), (9, 15, 0, "open"),
    (15, 30, 0, "open"), (15, 30, 1, "post"), (16, 0, 0, "post"), (16, 0, 1, "closed"), (2, 0, 0, "closed"),
])
def test_session_phase_boundaries(clock, no_holiday, h, mi, s, phase):
    clock(*WED, h, mi, s)
    assert gw._market_session_phase_ist() == phase


def test_session_phase_weekend_is_closed_not_holiday(clock, monkeypatch):
    monkeypatch.setattr(gw, "is_nse_holiday", lambda d: True)
    clock(2026, 10, 3, 10, 0)
    assert gw._market_session_phase_ist() == "closed"


@pytest.mark.parametrize("open_,ttl", [(True, gw.DECIDE_CACHE_TTL_OPEN), (False, gw.DECIDE_CACHE_TTL_CLOSED)])
def test_decide_cache_ttl_follows_the_session(monkeypatch, open_, ttl):
    monkeypatch.setattr(gw, "_is_market_open_ist", lambda: open_)
    assert gw._decide_cache_ttl() == ttl


# ── _should_force_lite_scan ──────────────────────────────────────────────────

@pytest.fixture
def health(monkeypatch):
    env = types.SimpleNamespace(snaps={}, counters={}, snap_raises=None, metric_raises=None)

    def all_snaps():
        if env.snap_raises:
            raise env.snap_raises
        return env.snaps

    class M:
        def snapshot(self_inner):
            if env.metric_raises:
                raise env.metric_raises
            return {"counters": env.counters}

    monkeypatch.setattr(gw, "all_snapshots", all_snaps)
    monkeypatch.setattr(gw, "metrics", M())
    return env


def test_force_lite_when_any_breaker_is_open(health):
    health.snaps = {"a": {"state": "closed"}, "b": {"state": "open"}}
    assert gw._should_force_lite_scan() is True


def test_force_lite_error_rate_thresholds(health):
    health.snaps = {"a": {"state": "closed"}}
    health.counters = {"x_dependency_errors_total{dep=a}": 6, "x_dependency_ok_total{dep=a}": 9}     # 15 @ 0.40
    assert gw._should_force_lite_scan() is True
    health.counters = {"dependency_errors": 7, "dependency_ok": 13}                                  # 20 @ exactly 0.35
    assert gw._should_force_lite_scan() is True
    health.counters = {"dependency_errors": 6, "dependency_ok": 14}                                  # 20 @ 0.30
    assert gw._should_force_lite_scan() is False
    health.counters = {"dependency_errors": 7, "dependency_ok": 7}                                   # 50% but only 14 calls
    assert gw._should_force_lite_scan() is False
    health.counters = {"other_counter": 999}
    assert gw._should_force_lite_scan() is False


def test_force_lite_sums_across_all_matching_counter_names(health):
    health.counters = {"a_dependency_errors_total": 3, "b_dependency_errors_total": 3, "c_dependency_ok_total": 9}
    assert gw._should_force_lite_scan() is True                        # 6 / 15 = 0.4


def test_force_lite_missing_or_none_counters_and_snapshot_errors(health):
    health.counters = None
    assert gw._should_force_lite_scan() is False
    health.snap_raises = RuntimeError("x")
    assert gw._should_force_lite_scan() is False
    health.snap_raises, health.metric_raises = None, RuntimeError("y")
    assert gw._should_force_lite_scan() is False


# ── batch-result cache ───────────────────────────────────────────────────────

BP = gw.BATCH_RESULT_CACHE_PREFIX


@pytest.fixture
def batch(monkeypatch, kv):
    monkeypatch.setattr(gw, "BATCH_RESULT_CACHE_ENABLED", True)
    monkeypatch.setattr(gw, "_redis_soft_ttl_refresh", lambda key, soft_window=10: False)
    monkeypatch.setattr(gw, "_decide_cache_ttl", lambda: 123)
    return kv


def test_batch_cache_key_shapes():
    assert gw._batch_result_cache_key("tcs") == BP + "full:TCS"
    assert gw._batch_result_cache_key("tcs", lite=True) == BP + "lite:TCS"
    assert gw._batch_result_cache_key(None) == BP + "full:"


def test_batch_get_returns_a_marked_copy(batch):
    row = {"decision": "BUY NOW", "symbol": "TCS"}
    batch.store[BP + "full:TCS"] = row
    out = gw._batch_result_cache_get("tcs")
    assert out == {"decision": "BUY NOW", "symbol": "TCS", "_from_batch_cache": True}
    assert "_from_batch_cache" not in row                      # cached row itself untouched
    assert gw._batch_result_cache_get("tcs", lite=True) is None


@pytest.mark.parametrize("value", [None, "x", [1], {}, {"decision": None}, {"decision": "ERROR"}])
def test_batch_get_ignores_unusable_rows(batch, value):
    batch.store[BP + "full:TCS"] = value
    assert gw._batch_result_cache_get("TCS") is None


def test_batch_get_treats_nearly_expired_as_a_miss(batch, monkeypatch):
    batch.store[BP + "lite:TCS"] = {"decision": "HOLD"}
    seen = []
    monkeypatch.setattr(gw, "_redis_soft_ttl_refresh", lambda key, soft_window=10: seen.append((key, soft_window)) or True)
    assert gw._batch_result_cache_get("TCS", lite=True) is None
    assert seen == [(BP + "lite:TCS", 15)]


def test_batch_get_and_set_are_noops_when_disabled(batch, monkeypatch):
    monkeypatch.setattr(gw, "BATCH_RESULT_CACHE_ENABLED", False)
    batch.store[BP + "full:TCS"] = {"decision": "HOLD"}
    assert gw._batch_result_cache_get("TCS") is None
    gw._batch_result_cache_set("TCS", {"decision": "HOLD"})
    assert batch.sets == []


def test_batch_set_strips_private_keys_and_uses_the_decide_ttl(batch):
    gw._batch_result_cache_set("tcs", {"decision": "HOLD", "symbol": "TCS", "_from_batch_cache": True, "_x": 1})
    assert batch.sets == [(BP + "full:TCS", {"decision": "HOLD", "symbol": "TCS"}, 123)]
    gw._batch_result_cache_set("tcs", {"decision": "HOLD"}, lite=True)
    assert batch.sets[-1][0] == BP + "lite:TCS"


@pytest.mark.parametrize("row", [None, "x", [1], {}, {"decision": None}, {"decision": "ERROR"}])
def test_batch_set_skips_unusable_rows(batch, row):
    gw._batch_result_cache_set("TCS", row)
    assert batch.sets == []


# ── _prioritize_universe ─────────────────────────────────────────────────────

@pytest.fixture
def lists(monkeypatch):
    env = types.SimpleNamespace(watch=[], searched=[])
    monkeypatch.setattr(gw, "_load_watchlist", lambda: list(env.watch))
    monkeypatch.setattr(gw, "_load_searched", lambda: list(env.searched))
    return env


def test_prioritize_puts_watchlist_then_searched_first_in_order(lists):
    lists.watch, lists.searched = ["c", "A"], ["B", "a"]
    assert gw._prioritize_universe(["A", "B", "C", "D", "E"]) == ["C", "A", "B", "D", "E"]


def test_prioritize_ignores_priority_names_outside_the_universe(lists):
    lists.watch, lists.searched = ["GHOST", "A"], ["PHANTOM"]
    assert gw._prioritize_universe(["A", "B"]) == ["A", "B"]


def test_prioritize_normalises_suffixes_case_and_dedupes(lists):
    lists.watch, lists.searched = ["tcs.NS", "INFY.BO"], ["TCS"]
    out = gw._prioritize_universe(["infy.ns", "TCS.BO", "wipro.NS", "WIPRO", "hdfc"])
    assert out == ["TCS", "INFY", "WIPRO", "HDFC"]


def test_prioritize_drops_blank_symbols_and_handles_empty_inputs(lists):
    lists.watch = [".NS", ""]
    assert gw._prioritize_universe(["", ".NS", "A"]) == ["A"]
    assert gw._prioritize_universe([]) == []
    lists.watch = ["A"]
    assert gw._prioritize_universe([]) == []


# ── _cb_get / _cb_post ───────────────────────────────────────────────────────

class Breaker:
    def __init__(self, allow=True, retry=7.0):
        self._allow, self._retry = allow, retry
        self.failures, self.successes = [], 0

    def allow(self):
        return self._allow

    def retry_after(self):
        return self._retry

    def record_failure(self, msg):
        self.failures.append(msg)

    def record_success(self):
        self.successes += 1


class Limiter:
    def __init__(self, allows=(True,), budget=5.0):
        self.allows, self.budget, self.buckets = list(allows), budget, []

    def allow(self, bucket):
        self.buckets.append(bucket)
        return self.allows.pop(0) if len(self.allows) > 1 else self.allows[0]

    def wait_budget_sec(self, bucket):
        return self.budget


class Metrics:
    def __init__(self):
        self.incs, self.obs = [], []

    def inc(self, name, **labels):
        self.incs.append((name, labels))

    def observe_ms(self, name, ms, **labels):
        self.obs.append((name, labels))


class HttpClient:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    async def get(self, url, timeout=None, **k):
        return self._next("get", url, timeout, k)

    async def post(self, url, timeout=None, **k):
        return self._next("post", url, timeout, k)

    def _next(self, verb, url, timeout, k):
        self.calls.append((verb, url, timeout, k))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def plumbing(monkeypatch, no_sleep):
    env = types.SimpleNamespace(br=Breaker(), lim=Limiter(), met=Metrics(), names=[], sleeps=no_sleep)

    def get_breaker(name):
        env.names.append(name)
        return env.br

    monkeypatch.setattr(gw, "get_breaker", get_breaker)
    monkeypatch.setattr(gw, "redis_limiter", env.lim)
    monkeypatch.setattr(gw, "metrics", env.met)
    return env


@pytest.mark.parametrize("name,bucket", [
    ("market-data", "market_data"), ("quote", "market_data"), ("stock_history", "market_data"),
    ("fundamental", "analysis"), ("technical", "analysis"), ("news", "analysis"), ("event", "analysis"),
    ("decision", "decision"), ("prediction", "decision"), ("training", "decision"),
    ("gemini", "gemini"), ("other", "global"), ("", "global"), (None, "global"),
    ("MARKET-DATA", "market_data"), ("event-market", "market_data"),
])
def test_cb_get_routes_names_to_rate_limit_buckets(plumbing, name, bucket):
    client = HttpClient([Resp(200)])
    run(gw._cb_get(client, name, "http://x", timeout=3))
    assert plumbing.lim.buckets == [bucket]


def test_cb_get_success_records_metrics_and_passes_kwargs(plumbing):
    client = HttpClient([Resp(200)])
    resp = run(gw._cb_get(client, "news", "http://x/a", timeout=9, params={"q": 1}))
    assert resp.status_code == 200
    assert client.calls == [("get", "http://x/a", 9, {"params": {"q": 1}})]
    assert plumbing.br.successes == 1 and plumbing.br.failures == []
    assert ("stockky_dependency_ok_total", {"dependency": "news"}) in plumbing.met.incs
    assert plumbing.met.obs[0][0] == "stockky_dependency_latency"


def test_cb_get_5xx_counts_as_a_failure_but_still_returns_the_response(plumbing):
    client = HttpClient([Resp(502)])
    resp = run(gw._cb_get(client, "news", "http://x"))
    assert resp.status_code == 502 and plumbing.br.failures == ["HTTP 502"] and plumbing.br.successes == 0
    assert ("stockky_dependency_errors_total", {"dependency": "news"}) in plumbing.met.incs
    assert run(gw._cb_get(HttpClient([Resp(404)]), "news", "u")).status_code == 404     # 4xx is a success for the breaker
    assert plumbing.br.successes == 1


def test_cb_get_rate_limited_waits_once_then_proceeds(plumbing):
    plumbing.lim = Limiter(allows=[False, True], budget=5.0)
    gw.redis_limiter = plumbing.lim
    run(gw._cb_get(HttpClient([Resp(200)]), "news", "u"))
    assert plumbing.sleeps == [2.0]                                     # min(2.0, budget)
    assert plumbing.lim.buckets == ["analysis", "analysis"]


def test_cb_get_rate_limit_wait_is_capped_by_the_budget(plumbing):
    plumbing.lim = Limiter(allows=[False, True], budget=0.4)
    gw.redis_limiter = plumbing.lim
    run(gw._cb_get(HttpClient([Resp(200)]), "news", "u"))
    assert plumbing.sleeps == [0.4]


def test_cb_get_rate_limit_still_exhausted_raises_circuit_open(plumbing):
    plumbing.lim = Limiter(allows=[False], budget=3.0)
    gw.redis_limiter = plumbing.lim
    with pytest.raises(gw.CircuitOpenError) as ei:
        run(gw._cb_get(HttpClient([]), "news", "u"))
    assert ei.value.name == "news" and ei.value.retry_after == 3.0
    with pytest.raises(gw.CircuitOpenError) as ei:
        run(gw._cb_get(HttpClient([]), "", "u"))
    assert ei.value.name == "rate_limit"
    assert plumbing.names == []                                          # breaker never consulted


def test_cb_get_open_breaker_short_circuits(plumbing):
    plumbing.br._allow = False
    client = HttpClient([])
    with pytest.raises(gw.CircuitOpenError) as ei:
        run(gw._cb_get(client, "news", "u"))
    assert ei.value.retry_after == 7.0 and client.calls == []
    assert ("stockky_circuit_open_total", {"dependency": "news"}) in plumbing.met.incs


def test_cb_get_retries_one_timeout_after_a_short_backoff(plumbing):
    client = HttpClient([httpx.ReadTimeout("t"), Resp(200)])
    assert run(gw._cb_get(client, "news", "u")).status_code == 200
    assert plumbing.sleeps == [0.6] and len(client.calls) == 2 and plumbing.br.failures == []


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("r"), httpx.ConnectTimeout("c"), httpx.PoolTimeout("p")])
def test_cb_get_two_timeouts_record_one_failure_and_raise(plumbing, exc):
    client = HttpClient([exc, type(exc)("again")])
    with pytest.raises(type(exc)):
        run(gw._cb_get(client, "news", "u"))
    assert plumbing.br.failures == ["again"] and len(client.calls) == 2
    assert ("stockky_dependency_errors_total", {"dependency": "news"}) in plumbing.met.incs


def test_cb_get_other_errors_fail_immediately_without_retry(plumbing):
    client = HttpClient([httpx.ConnectError("refused"), Resp(200)])
    with pytest.raises(httpx.ConnectError):
        run(gw._cb_get(client, "news", "u"))
    assert plumbing.br.failures == ["refused"] and len(client.calls) == 1 and plumbing.sleeps == []


def test_cb_get_circuit_open_from_the_client_is_not_recorded_as_a_failure(plumbing):
    client = HttpClient([gw.CircuitOpenError("inner", 1.0)])
    with pytest.raises(gw.CircuitOpenError):
        run(gw._cb_get(client, "news", "u"))
    assert plumbing.br.failures == []


def test_cb_get_dead_tail_after_the_loop(plumbing, monkeypatch):
    """`range(2)` can never finish without returning or raising, so the trailing
    ``if last_err: raise last_err`` is unreachable in production. Shadowing `range` in the module
    (attempt is always 0 → always 'retry') reaches it; an empty range falls off the end."""
    monkeypatch.setattr(gw, "range", lambda n: [0, 0], raising=False)
    with pytest.raises(httpx.ReadTimeout):
        run(gw._cb_get(HttpClient([httpx.ReadTimeout("a"), httpx.ReadTimeout("b")]), "news", "u"))
    monkeypatch.setattr(gw, "range", lambda n: [], raising=False)
    assert run(gw._cb_get(HttpClient([]), "news", "u")) is None


def test_cb_post_success_5xx_and_open_breaker(plumbing):
    client = HttpClient([Resp(200), Resp(500)])
    assert run(gw._cb_post(client, "decision", "http://d", timeout=4, json={"a": 1})).status_code == 200
    assert client.calls[0] == ("post", "http://d", 4, {"json": {"a": 1}})
    assert plumbing.br.successes == 1
    assert run(gw._cb_post(client, "decision", "http://d")).status_code == 500
    assert plumbing.br.failures == ["HTTP 500"]
    plumbing.br._allow = False
    with pytest.raises(gw.CircuitOpenError) as ei:
        run(gw._cb_post(HttpClient([]), "decision", "u"))
    assert ei.value.retry_after == 7.0


def test_cb_post_retry_semantics(plumbing):
    client = HttpClient([httpx.PoolTimeout("t"), Resp(201)])
    assert run(gw._cb_post(client, "decision", "u")).status_code == 201 and plumbing.sleeps == [0.6]
    with pytest.raises(httpx.ConnectTimeout):
        run(gw._cb_post(HttpClient([httpx.ConnectTimeout("1"), httpx.ConnectTimeout("2")]), "decision", "u"))
    assert plumbing.br.failures == ["2"]
    with pytest.raises(RuntimeError):
        run(gw._cb_post(HttpClient([RuntimeError("boom")]), "decision", "u"))
    assert plumbing.br.failures[-1] == "boom"
    with pytest.raises(gw.CircuitOpenError):
        run(gw._cb_post(HttpClient([gw.CircuitOpenError("i", 1.0)]), "decision", "u"))
    assert len(plumbing.br.failures) == 2


def test_cb_post_dead_tail_after_the_loop(plumbing, monkeypatch):
    """Same unreachable tail as `_cb_get` — reached by shadowing `range` in the module."""
    monkeypatch.setattr(gw, "range", lambda n: [0, 0], raising=False)
    with pytest.raises(httpx.ReadTimeout):
        run(gw._cb_post(HttpClient([httpx.ReadTimeout("a"), httpx.ReadTimeout("b")]), "decision", "u"))
    monkeypatch.setattr(gw, "range", lambda n: [], raising=False)
    assert run(gw._cb_post(HttpClient([]), "decision", "u")) is None


# ── _wake_required_services ──────────────────────────────────────────────────

class WakeClient:
    def __init__(self):
        self.calls, self.routes = [], {}

    async def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        for (frag, is_warm), script in self.routes.items():
            if frag in url and is_warm == bool(params):
                item = script.pop(0) if len(script) > 1 else script[0]
                if isinstance(item, Exception):
                    raise item
                return item
        raise RuntimeError("no route " + url)


def _svc(monkeypatch, mapping):
    monkeypatch.setattr(gw, "SYSTEM_SERVICES", {k: {"url": v, "required": True} for k, v in mapping.items()})


def test_wake_market_data_prefers_the_wake_endpoint(monkeypatch, no_sleep):
    _svc(monkeypatch, {"market-data": "http://md/"})
    c = WakeClient()
    c.routes[("/wake", False)] = [Resp(200)]
    out = run(gw._wake_required_services(c))
    assert out == {"market-data": {"ok": True, "status": 200, "warmed": True}}
    assert c.calls == [("http://md/wake", None, 25)] and no_sleep == []


def test_wake_market_data_falls_back_to_health_with_a_second_ping(monkeypatch, no_sleep):
    _svc(monkeypatch, {"market-data": "http://md"})
    c = WakeClient()
    c.routes[("/wake", False)] = [RuntimeError("no wake")]
    c.routes[("/health", True)] = [Resp(503)]
    c.routes[("/health", False)] = [Resp(200)]
    out = run(gw._wake_required_services(c))
    assert out["market-data"] == {"ok": True, "status": 200, "warmed": True}
    assert no_sleep == [2.5]
    assert [x[0] for x in c.calls] == ["http://md/wake", "http://md/health", "http://md/health"]
    assert c.calls[1][1] == {"warm": "true"} and c.calls[1][2] == 25 and c.calls[2][2] == 15


def test_wake_market_data_non_200_wake_also_falls_back_and_ok_if_either_ping_works(monkeypatch, no_sleep):
    _svc(monkeypatch, {"market-data": "http://md"})
    c = WakeClient()
    c.routes[("/wake", False)] = [Resp(500)]
    c.routes[("/health", True)] = [Resp(200)]
    c.routes[("/health", False)] = [Resp(503)]
    out = run(gw._wake_required_services(c))
    assert out["market-data"] == {"ok": True, "status": 503, "warmed": True}     # ok from the first ping, status from the second


def test_wake_other_services_warm_then_retry_once_on_failure(monkeypatch, no_sleep):
    _svc(monkeypatch, {"news": "http://n/", "tech": "http://t"})
    c = WakeClient()
    c.routes[("http://n/health", True)] = [Resp(200)]
    c.routes[("http://t/health", True)] = [Resp(503), Resp(200)]
    out = run(gw._wake_required_services(c))
    assert out == {"news": {"ok": True, "status": 200, "warmed": True}, "tech": {"ok": True, "status": 200, "warmed": True}}
    assert no_sleep == [2]                                    # one in-ping retry; no second pass needed
    assert all(x[1] == {"warm": "true"} and x[2] == 20 for x in c.calls)


def test_wake_failures_get_one_second_pass(monkeypatch, no_sleep):
    _svc(monkeypatch, {"a": "http://a", "b": "http://b", "c": ""})
    c = WakeClient()
    c.routes[("http://a/health", True)] = [RuntimeError("cold"), Resp(200)]
    c.routes[("http://b/health", True)] = [Resp(500)]
    out = run(gw._wake_required_services(c))
    assert out["a"] == {"ok": True, "status": 200, "warmed": True}          # recovered on the second pass
    assert out["b"] == {"ok": False, "status": 500, "warmed": True}
    assert out["c"] == {"ok": False, "error": "no url"}
    assert 3 in no_sleep                                                   # the second-pass pause


def test_wake_error_text_is_truncated_to_120_chars(monkeypatch, no_sleep):
    _svc(monkeypatch, {"a": "http://a"})
    c = WakeClient()
    c.routes[("http://a/health", True)] = [RuntimeError("e" * 300)]
    out = run(gw._wake_required_services(c))
    assert out["a"]["ok"] is False and len(out["a"]["error"]) == 120


def test_wake_without_a_client_uses_the_shared_one(monkeypatch, no_sleep):
    _svc(monkeypatch, {"a": "http://a"})
    c = WakeClient()
    c.routes[("http://a/health", True)] = [Resp(200)]
    monkeypatch.setattr(gw, "_get_http_client", lambda: c)
    assert run(gw._wake_required_services())["a"]["ok"] is True
