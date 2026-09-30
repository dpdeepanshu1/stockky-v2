"""tests/test_main_analyze_symbol.py — coverage for api-gateway/main.py, slice 3b (lines 2924-3313)

Pass 61 (second half). `_analyze_one_symbol_ultra`, the per-symbol worker every scan and
single-stock Analyse goes through:

* feed-row resolution (explicit row, bulk-prefetched dict, per-symbol Neon read) and the
  process-local warm cache;
* the lite fast-path (feed price ladder, quote fallback, score ladder) and its fall-through;
* the decide cache, POST /decide/evaluate (feed has data) vs GET /decide/{symbol};
* the durable-feed overlay onto the decision row and the price fallbacks;
* the non-lite fundamental / event / news / prediction enrichment;
* holding-period estimate and Gemini / template summary selection;
* the three failure lanes (circuit open, httpx error with one retry, unexpected error).

Everything downstream is faked: `_cb_get` / `_cb_post`, the KV helpers, the Data Feed store, the
four `_fetch_*_cached` helpers, `_generate_ai_summary`, `_fetch_price_from_quote` and metrics.
`asyncio.sleep` is faked so the retry / circuit pauses cost nothing. The "Max retries exceeded"
tail is unreachable with the real ``range(MAX_RETRIES + 1)``; the test shadows ``range`` inside
the module to reach it and says so. Findings are pinned as current behaviour, ``NOT FIXED``.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_analyze_symbol.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

import httpx
import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)

import data_feed as _df


# ── fakes ────────────────────────────────────────────────────────────────────

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


class DResp:
    """Decision-engine response."""

    def __init__(self, payload=None, status_error=None):
        self._payload, self._status_error = payload, status_error

    def raise_for_status(self):
        if self._status_error is not None:
            raise self._status_error

    def json(self):
        return self._payload


class Env:
    """One place to script every collaborator of `_analyze_one_symbol_ultra`.

    Setting `get_result` scripts BOTH decision routes (GET /decide/{sym} and POST /decide/evaluate),
    so a test does not need to know which one a feed row selects; set `post_result` afterwards to
    script the POST route on its own.
    """

    @property
    def get_result(self):
        return self._get

    @get_result.setter
    def get_result(self, value):
        self._get = self._post = value

    @property
    def post_result(self):
        return self._post

    @post_result.setter
    def post_result(self, value):
        self._post = value

    def __init__(self):
        self.kv, self.kv_sets = {}, []
        self.feed_rows, self.feed_get_raises, self.feed_gets = {}, None, []
        self.get_calls, self.post_calls = [], []
        self._get = self._post = DResp({"decision": "HOLD", "combined_score": 55})
        self.written = set()
        self.get_exc = self.post_exc = None
        self.price, self.price_raises, self.price_calls = None, None, []
        self.fund = ({}, True)
        self.event = self.news = None
        self.pred = (None, None)
        self.fund_exc = self.event_exc = self.news_exc = self.pred_exc = None
        self.fetch_calls = []
        self.ai = "AI summary."
        self.ai_calls = []
        self.sleeps, self.incs = [], []


@pytest.fixture
def log(monkeypatch):
    rec = RecLogger()
    monkeypatch.setattr(gw, "logger", rec)
    return rec


@pytest.fixture
def env(monkeypatch):
    e = Env()

    monkeypatch.setattr(gw, "_redis_get", lambda k: e.kv.get(k))

    def rset(k, v, ttl=None):
        e.kv_sets.append((k, v, ttl))
        e.kv[k] = v
        e.written.add(k)

    monkeypatch.setattr(gw, "_redis_set", rset)
    monkeypatch.setattr(gw, "_decide_cache_ttl", lambda: 77)

    class Store:
        def get_symbol(self_inner, s):
            e.feed_gets.append(s)
            if e.feed_get_raises is not None:
                raise e.feed_get_raises
            return e.feed_rows.get(s)

    monkeypatch.setattr(gw, "_feed_store", lambda: Store())

    async def cb_get(client, name, url, timeout=5.0, **k):
        e.get_calls.append((name, url, timeout))
        if e.get_exc is not None:
            raise e.get_exc
        return e.get_result

    async def cb_post(client, name, url, timeout=8.0, **k):
        e.post_calls.append((name, url, timeout, k.get("json")))
        if e.post_exc is not None:
            raise e.post_exc
        return e.post_result

    monkeypatch.setattr(gw, "_cb_get", cb_get)
    monkeypatch.setattr(gw, "_cb_post", cb_post)

    def price(sym):
        e.price_calls.append(sym)
        if e.price_raises is not None:
            raise e.price_raises
        return e.price

    monkeypatch.setattr(gw, "_fetch_price_from_quote", price)

    def make_fetch(name, attr, exc_attr):
        async def fetch(symbol, client):
            e.fetch_calls.append(name)
            if getattr(e, exc_attr) is not None:
                raise getattr(e, exc_attr)
            return getattr(e, attr)
        return fetch

    monkeypatch.setattr(gw, "_fetch_fundamental_cached", make_fetch("fund", "fund", "fund_exc"))
    monkeypatch.setattr(gw, "_fetch_events_cached", make_fetch("event", "event", "event_exc"))
    monkeypatch.setattr(gw, "_fetch_news_cached", make_fetch("news", "news", "news_exc"))
    monkeypatch.setattr(gw, "_fetch_prediction_cached", make_fetch("pred", "pred", "pred_exc"))

    async def ai(data, client):
        e.ai_calls.append(data.get("symbol"))
        return e.ai

    monkeypatch.setattr(gw, "_generate_ai_summary", ai)

    async def fake_sleep(delay, *a, **k):
        e.sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    class M:
        def inc(self_inner, name, **labels):
            e.incs.append((name, labels))

        def observe_ms(self_inner, *a, **k):
            pass

    monkeypatch.setattr(gw, "metrics", M())

    # keep the process-local Data Feed cache from leaking between tests
    saved_syms, saved_idx = dict(_df._LOCAL_SYMBOLS), set(_df._LOCAL_INDEX)
    _ENV_HOLDER["env"] = e
    yield e
    _ENV_HOLDER.pop("env", None)
    _df._LOCAL_SYMBOLS.clear()
    _df._LOCAL_SYMBOLS.update(saved_syms)
    _df._LOCAL_INDEX.clear()
    _df._LOCAL_INDEX.update(saved_idx)


_ENV_HOLDER = {}


def analyze(symbol="TCS", **kw):
    e = _ENV_HOLDER.get("env")
    if e is not None:                       # a second analyze() in one test must not hit the first one's decide cache
        for k in e.written:
            e.kv.pop(k, None)
        e.written.clear()

    async def go():
        sem = asyncio.Semaphore(2)
        return await gw._analyze_one_symbol_ultra(symbol, object(), sem, **kw)

    return asyncio.run(go())


FULL_ROW = {"rsi": 55, "pe": 22, "technical_score": 60, "fundamental_score": 70, "news_score": 40,
            "close": 100.0, "sector": "IT", "metrics": {"roe": 20}, "updated_at": "U"}


# ── feed-row resolution ──────────────────────────────────────────────────────

def test_explicit_feed_row_skips_every_lookup(env):
    env.get_result = DResp({"decision": "BUY NOW", "combined_score": 80, "close": 100.0})
    analyze("TCS", feed_row={"sector": "IT"}, prefetched_feeds={"TCS": {"sector": "OTHER"}})
    assert env.feed_gets == []


def test_prefetched_bulk_row_is_used_and_normalised_symbol_looked_up(env):
    env.get_result = DResp({"decision": "HOLD", "close": 10.0})
    out = analyze("tcs.NS", prefetched_feeds={"TCS": {"sector": "IT", "updated_at": "U"}})
    assert env.feed_gets == [] and out["sector"] == "IT" and out["data_feed_updated_at"] == "U"


def test_prefetched_miss_falls_back_to_the_per_symbol_neon_read(env):
    env.feed_rows["TCS"] = {"sector": "FIN"}
    env.get_result = DResp({"decision": "HOLD", "close": 10.0})
    out = analyze(" tcs.BO ", prefetched_feeds={"OTHER": {}})
    assert env.feed_gets == ["TCS"] and out["sector"] == "FIN"


def test_feed_store_error_means_no_feed_row(env):
    env.feed_get_raises = RuntimeError("neon down")
    env.get_result = DResp({"decision": "HOLD", "close": 10.0})
    out = analyze("TCS")
    assert out["decision"] == "HOLD" and "data_feed_updated_at" not in out


def test_prefetched_feeds_that_is_not_a_dict_is_ignored(env):
    env.get_result = DResp({"decision": "HOLD", "close": 10.0})
    analyze("TCS", prefetched_feeds=["x"])
    assert env.feed_gets == ["TCS"]


def test_blank_symbol_does_not_consult_prefetched_feeds(env):
    env.get_result = DResp({"decision": "HOLD", "close": 10.0})
    analyze("", prefetched_feeds={"": {"sector": "X"}})
    assert env.feed_gets == [""]


def test_non_empty_feed_row_warms_the_process_local_cache(env):
    env.get_result = DResp({"decision": "HOLD", "close": 10.0})
    analyze("WARMME", feed_row={"sector": "IT"})
    assert _df._LOCAL_SYMBOLS[_df.DATA_FEED_PREFIX + "WARMME"] == {"sector": "IT"}
    assert "WARMME" in _df._LOCAL_INDEX


def test_empty_feed_row_is_not_cached_locally(env):
    env.get_result = DResp({"decision": "HOLD", "close": 10.0})
    analyze("EMPTYROW", feed_row={})
    assert _df.DATA_FEED_PREFIX + "EMPTYROW" not in _df._LOCAL_SYMBOLS


def test_local_cache_warm_failure_is_swallowed(env, monkeypatch):
    monkeypatch.delattr(_df, "_LOCAL_SYMBOLS")                        # `from data_feed import ...` now fails
    env.get_result = DResp({"decision": "HOLD", "close": 10.0})
    try:
        assert analyze("TCS", feed_row={"sector": "IT"})["decision"] == "HOLD"
    finally:
        _df._LOCAL_SYMBOLS = {}


# ── lite fast-path ───────────────────────────────────────────────────────────

def test_lite_uses_the_feed_row_without_touching_decision_or_quote(env):
    out = analyze("TCS", lite=True, feed_row={"close": "123.5", "combined_score": 61, "decision": "PREPARE TO BUY",
                                              "confidence": "High", "sector": "IT", "metrics": {"a": 1},
                                              "fundamental_score": 70})
    assert out["decision"] == "PREPARE TO BUY" and out["combined_score"] == 61.0 and out["confidence"] == "High"
    assert out["close"] == 123.5 and out["support"] == round(123.5 * 0.95, 2) and out["resistance"] == round(123.5 * 1.05, 2)
    assert out["lite_fastpath"] is True and out["from_data_feed"] is True and out["fundamental_metrics"] == {"a": 1}
    assert out["natural_language_summary"] == "TCS: lite path — decision=PREPARE TO BUY score=61.0 close=123.5"
    assert out["reasons"]["lite"][0].startswith("Lite scan")
    assert env.get_calls == [] and env.post_calls == [] and env.price_calls == []


@pytest.mark.parametrize("row,expected", [
    ({"close": 1, "price": 2}, 1.0),
    ({"close": "bad", "price": 2}, 2.0),
    ({"close": None, "price": None, "ltp": 3}, 3.0),
    ({"close": [1], "prev_close": 4}, 4.0),
    ({"last": "5", "fundamental_score": 1}, 5.0),
])
def test_lite_feed_price_ladder(env, row, expected):
    assert analyze("TCS", lite=True, feed_row=row)["close"] == expected
    assert env.price_calls == []


def test_lite_falls_back_to_the_quote_when_the_feed_has_no_usable_price(env):
    env.price = 88.0
    out = analyze("TCS", lite=True, feed_row={"close": "bad", "fundamental_score": 66})
    assert out["close"] == 88.0 and env.price_calls == ["TCS"]
    assert out["combined_score"] == 66.0                                # fundamental_score is next on the score ladder


def test_lite_quote_only_returns_a_hold_at_score_50(env):
    env.price = 42.0
    out = analyze("TCS", lite=True)
    assert (out["decision"], out["combined_score"], out["confidence"]) == ("HOLD", 50.0, "Low")
    assert out["close"] == 42.0 and out["from_data_feed"] is False
    assert out["support"] == 39.9 and out["resistance"] == 44.1


def test_lite_quote_error_is_treated_as_no_price(env):
    env.price_raises = RuntimeError("md down")
    env.get_result = DResp({"decision": "DO NOT BUY", "combined_score": 20})
    out = analyze("TCS", lite=True)
    assert env.get_calls and out["decision"] == "DO NOT BUY"          # fell through to the decision engine


@pytest.mark.parametrize("row,score", [
    ({"combined_score": 0, "fundamental_score": 0, "technical_score": 33, "close": 1}, 33.0),
    ({"combined_score": 0, "fundamental_score": 0, "technical_score": 0, "close": 1}, 50.0),
    ({"combined_score": "abc", "close": 1}, 50.0),
    ({"combined_score": 72.5, "close": 1}, 72.5),
])
def test_lite_score_ladder_treats_zero_as_missing(env, row, score):
    assert analyze("TCS", lite=True, feed_row=row)["combined_score"] == score   # NOT FIXED: a real 0 score becomes the next rung


def test_lite_feed_with_only_a_decision_string_still_takes_the_fast_path(env):
    env.price = None
    out = analyze("TCS", lite=True, feed_row={"decision": "SELL"})
    assert out["decision"] == "SELL" and out["close"] is None and out["support"] is None
    assert env.get_calls == []


def test_lite_with_nothing_to_go_on_falls_through_to_the_decision_engine(env):
    env.price = None
    env.get_result = DResp({"decision": "HOLD", "combined_score": 51, "close": 9.0})
    out = analyze("TCS", lite=True)
    assert env.get_calls == [("decision", f"{gw.DECISION_URL}/decide/TCS", 15)]
    assert "lite_fastpath" not in out and env.fetch_calls == []      # lite: no enrichment
    assert out["natural_language_summary"].startswith("🔄 TCS") and env.ai_calls == []


def test_lite_fastpath_close_that_cannot_be_floated_skips_support_and_resistance(env, monkeypatch):
    monkeypatch.setattr(gw, "_normalize_decision_response", lambda raw, sym: {**raw, "close": "n/a"})
    out = analyze("TCS", lite=True, feed_row={"close": 5})
    assert out["close"] == "n/a" and "support" not in out


def test_lite_fastpath_exception_is_logged_and_the_decision_path_takes_over(env, monkeypatch, log):
    real = gw._normalize_decision_response
    calls = {"n": 0}

    def flaky(raw, sym):
        calls["n"] += 1
        if raw.get("lite_fastpath"):
            raise RuntimeError("boom")
        return real(raw, sym)

    monkeypatch.setattr(gw, "_normalize_decision_response", flaky)
    env.get_result = DResp({"decision": "HOLD", "combined_score": 50, "close": 9.0})
    out = analyze("TCS", lite=True, feed_row={"close": 5})
    assert log.any("debug", "lite fastpath TCS: boom") and env.post_calls and out["decision"] == "HOLD"


def test_lite_fastpath_fills_a_missing_close_from_the_quote_price(env, monkeypatch):
    real = gw._normalize_decision_response
    monkeypatch.setattr(gw, "_normalize_decision_response",
                        lambda raw, sym: {**real(raw, sym), "close": None})
    env.price = 30.0
    out = analyze("TCS", lite=True)
    assert out["close"] == 30.0 and out["support"] == 28.5


# ── decide cache / decision engine ───────────────────────────────────────────

DKEY = gw.DECIDE_CACHE_PREFIX + "TCS"


def test_decide_cache_hit_skips_the_decision_engine(env):
    env.kv[DKEY] = {"decision": "BUY NOW", "symbol": "TCS", "combined_score": 90, "close": 10.0,
                    "fundamental_metrics": {"a": 1}, "event_data": {"x": 1}, "news_score": 1, "prediction_score": 1}
    out = analyze("TCS")
    assert out["from_decide_cache"] is True and out["decision"] == "BUY NOW"
    assert env.get_calls == [] and env.post_calls == [] and env.fetch_calls == []


@pytest.mark.parametrize("cached", [{}, {"decision": None}, {"combined_score": 5}, "x", ["d"]])
def test_unusable_decide_cache_goes_to_the_engine(env, cached):
    env.kv[DKEY] = cached
    analyze("TCS")
    assert len(env.get_calls) == 1


def test_no_feed_data_uses_get_decide_and_caches_the_result(env):
    env.get_result = DResp({"decision": "BUY NOW", "combined_score": 88, "close": 50.0})
    out = analyze("TCS")
    assert env.get_calls == [("decision", f"{gw.DECISION_URL}/decide/TCS", 15)] and env.post_calls == []
    key, cached, ttl = env.kv_sets[0]
    assert key == DKEY and ttl == 77 and cached["decision"] == "BUY NOW"
    assert out["holding_period_estimate"] is None                     # no target from the engine


def test_feed_with_data_posts_a_trimmed_evaluate_payload(env):
    row = dict(FULL_ROW, pe_ratio=None, market_score=44, support=90, resistance=120, events={"e": 1},
               event_risk=False, valuation="fair", ltp=None, empty=None)
    env.post_result = DResp({"decision": "HOLD", "combined_score": 61, "close": 100.0})
    analyze("tcs.NS", feed_row=row)
    (name, url, timeout, payload), = env.post_calls
    assert (name, url, timeout) == ("decision", f"{gw.DECISION_URL}/decide/evaluate", 20) and env.get_calls == []
    assert payload["symbol"] == "TCS" and payload["skip_http"] is True
    assert payload["pe_ratio"] == 22 and payload["sentiment_score"] == 44 and payload["close"] == 100.0
    assert payload["metrics"] == {"roe": 20} and payload["events"] == {"e": 1} and payload["event_risk"] is False
    assert "sentiment_score" in payload and "empty" not in payload
    assert not any(v is None for v in payload.values())


def test_evaluate_payload_field_fallbacks(env):
    env.post_result = DResp({"decision": "HOLD", "close": 1.0})
    analyze("TCS", feed_row={"pe_ratio": 15, "pe": 99, "sentiment_score": 7, "market_score": 8, "price": 12.0})
    p = env.post_calls[0][3]
    assert p["pe_ratio"] == 15 and p["sentiment_score"] == 7 and p["close"] == 12.0
    env.post_calls.clear()
    analyze("TCS", feed_row={"cmp": 13.0, "rsi": 40})
    assert env.post_calls[0][3]["close"] == 13.0


def test_feed_row_with_nothing_scoreable_uses_the_plain_get(env):
    analyze("TCS", feed_row={"sector": "IT", "updated_at": "U"})
    assert env.post_calls == [] and len(env.get_calls) == 1


def test_symbol_falls_back_to_the_raw_symbol_when_base_is_blank(env):
    env.post_result = DResp({"decision": "HOLD", "close": 1.0})
    analyze(" .NS ", feed_row={"rsi": 50})
    assert env.post_calls[0][3]["symbol"] == " .NS "


def test_status_error_from_the_engine_becomes_an_error_row_after_one_retry(env, log):
    req = httpx.Request("GET", "http://d")
    err = httpx.HTTPStatusError("503", request=req, response=httpx.Response(503, request=req))
    env.get_result = DResp(status_error=err)
    out = analyze("TCS")
    assert out == {"symbol": "TCS", "decision": "ERROR", "error": "HTTPStatusError: 503"}
    assert len(env.get_calls) == 2 and env.sleeps == [1.0]
    assert log.any("warning", "Scan error for TCS (attempt 1/2): HTTPStatusError - 503")
    assert log.any("info", "Retrying TCS in 1.0s...")
    assert env.kv_sets == []                                          # failures are never cached


def test_a_transient_http_error_is_retried_and_can_recover(env, monkeypatch):
    calls = {"n": 0}
    good = env.get_result

    async def flaky(client, name, url, timeout=5.0, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow")
        return good

    monkeypatch.setattr(gw, "_cb_get", flaky)
    out = analyze("TCS")
    assert out["decision"] == "HOLD" and calls["n"] == 2 and env.sleeps == [1.0]


def test_empty_http_error_message_gets_a_type_based_description(env):
    env.get_exc = httpx.ConnectError("")
    out = analyze("TCS")
    assert out["error"] == "ConnectError: ConnectError (empty message)"


def test_unexpected_errors_are_not_retried(env, log):
    env.get_exc = KeyError("boom")
    out = analyze("TCS")
    assert out == {"symbol": "TCS", "decision": "ERROR", "error": "Unexpected: KeyError - 'boom'"}
    assert len(env.get_calls) == 1 and env.sleeps == []
    assert log.any("error", "Unexpected error for TCS: KeyError")


def test_max_retries_tail_is_unreachable_unless_range_is_shadowed(env, monkeypatch):
    """With the real range(MAX_RETRIES + 1) every attempt returns or continues into a final
    return, so `return {... "Max retries exceeded"}` is dead code. An empty range reaches it."""
    monkeypatch.setattr(gw, "range", lambda n: [], raising=False)
    assert analyze("TCS") == {"symbol": "TCS", "decision": "ERROR", "error": "Max retries exceeded"}


def test_zero_retries_returns_the_error_on_the_first_failure(env, monkeypatch):
    monkeypatch.setattr(gw, "MAX_RETRIES", 0)
    env.get_exc = httpx.ReadTimeout("slow")
    assert analyze("TCS")["error"] == "ReadTimeout: slow" and len(env.get_calls) == 1 and env.sleeps == []


def test_circuit_open_returns_a_data_insufficient_row_with_a_price(env, log):
    env.get_exc = gw.CircuitOpenError("decision", 45.0)
    env.price = 12.5
    out = analyze("TCS")
    assert out["decision"] == "DO NOT BUY" and out["combined_score"] == 0 and out["data_insufficient"] is True
    assert out["circuit_open"] == "decision" and out["close"] == 12.5 and "circuit open" in out["error"]
    assert out["reasons"]["data_quality"] == ["Circuit open for decision; retry in 45s — price may still show"]
    assert env.sleeps == [1.5, 2.0]                                    # min(2.0, 45/10) → capped at 2.0
    assert ("stockky_scan_circuit_skip_total", {"dependency": "decision"}) in env.incs
    assert log.any("warning", "Scan skip TCS")
    assert len(env.get_calls) == 1                                     # fail fast: no retry


def test_circuit_open_pause_scales_with_retry_after_and_price_errors_are_swallowed(env):
    env.get_exc = gw.CircuitOpenError("decision", 5.0)
    env.price_raises = RuntimeError("x")
    out = analyze("TCS")
    assert out["close"] is None and env.sleeps == [1.5, 0.5]


def test_circuit_open_with_zero_retry_after_uses_the_two_second_default(env):
    err = gw.CircuitOpenError("decision", 0.0)
    env.get_exc = err
    analyze("TCS")
    assert env.sleeps == [1.5, 0.2]


# ── durable-feed overlay ─────────────────────────────────────────────────────

def test_overlay_fills_only_the_gaps_the_decision_left(env):
    env.get_result = DResp({"decision": "HOLD", "fundamental_score": None, "event_risk": None, "news_score": None,
                            "close": 10.0, "sector": "KEEP", "prediction_score": 1, "event_data": None})
    row = {"metrics": {"roe": 1}, "fundamental_score": 64, "sector": "IGNORED", "industry": "Soft",
           "valuation": "cheap", "quality_score": 7, "multi_quarter_score": 8, "events": {"x": 1},
           "event_risk": True, "news_score": 30, "updated_at": "U", "rsi": 50}
    out = analyze("TCS", feed_row=row)
    assert out["from_data_feed"] is True and out["data_feed_updated_at"] == "U"
    assert out["fundamental_metrics"] == {"roe": 1} and out["fundamental_score"] == 64
    assert out["sector"] == "KEEP" and out["industry"] == "Soft" and out["valuation"] == "fair"     # default "fair" is not None
    assert out["quality_score"] == 7 and out["multi_quarter_score"] == 8
    assert out["event_data"] == {"x": 1} and out["event_risk"] is True and out["news_score"] == 30


def test_overlay_event_data_falls_back_to_the_event_risk_flag(env):
    env.get_result = DResp({"decision": "HOLD", "close": 10.0})
    out = analyze("TCS", feed_row={"event_risk": False, "rsi": 50})
    assert out["event_data"] == {"event_risk": False}


def test_overlay_keeps_existing_event_data_and_decision_values(env):
    env.get_result = DResp({"decision": "HOLD", "close": 10.0, "event_data": {"mine": 1}, "fundamental_score": 12,
                            "fundamental_metrics": {"mine": 2}, "news_score": 5, "event_risk": True})
    out = analyze("TCS", feed_row={"events": {"theirs": 1}, "metrics": {"theirs": 2}, "fundamental_score": 99,
                                   "news_score": 99, "event_risk": False, "rsi": 1})
    assert out["event_data"] == {"mine": 1} and out["fundamental_score"] == 12
    assert out["fundamental_metrics"] == {"mine": 2} and out["news_score"] == 5 and out["event_risk"] is True


def test_overlay_does_not_invent_event_data_when_the_feed_has_none(env):
    env.get_result = DResp({"decision": "HOLD", "close": 10.0})
    out = analyze("TCS", feed_row={"rsi": 50})
    assert "event_data" not in out


def test_overlay_price_ladder_when_the_decision_has_no_close(env):
    env.get_result = DResp({"decision": "HOLD"})
    out = analyze("TCS", feed_row={"close": "bad", "price": None, "last": "7.5", "ltp": 9, "rsi": 1})
    assert out["close"] == 7.5 and env.price_calls == []
    env.get_result = DResp({"decision": "HOLD"})
    out = analyze("TCS", feed_row={"close": "bad", "price": "worse", "rsi": 1})
    assert env.price_calls == ["TCS"] and out["close"] is None         # nothing floatable → quote path (which has no price)


def test_overlay_skipped_for_an_empty_or_missing_feed_row(env):
    env.get_result = DResp({"decision": "HOLD", "close": 10.0})
    out = analyze("TCS", feed_row={})
    assert "from_data_feed" not in out and "data_feed_updated_at" not in out


# ── price fallback ───────────────────────────────────────────────────────────

def test_quote_price_fills_close_support_and_resistance(env):
    env.get_result = DResp({"decision": "HOLD"})
    env.price = 200.0
    out = analyze("TCS")
    assert out["close"] == 200.0 and out["support"] == 190.0 and out["resistance"] == 210.0


def test_quote_price_keeps_support_and_resistance_the_engine_supplied(env):
    env.get_result = DResp({"decision": "HOLD", "support": 1.0, "resistance": 2.0})
    env.price = 200.0
    out = analyze("TCS")
    assert (out["support"], out["resistance"]) == (1.0, 2.0)


def test_no_price_anywhere_leaves_close_empty(env):
    env.get_result = DResp({"decision": "HOLD"})
    env.price_raises = RuntimeError("x")
    assert analyze("TCS")["close"] is None
    env.price_raises, env.price = None, None
    assert analyze("TCS")["close"] is None


# ── enrichment (non-lite) ────────────────────────────────────────────────────

ALL_SUPPLIED = {"decision": "HOLD", "close": 10.0, "fundamental_metrics": {"a": 1}, "event_data": {"x": 1},
                "news_score": 1, "prediction_score": 1}


def test_nothing_is_fetched_when_the_engine_already_supplied_every_pillar(env):
    env.get_result = DResp(dict(ALL_SUPPLIED))
    analyze("TCS")
    assert env.fetch_calls == []


def test_event_risk_alone_also_suppresses_the_event_fetch(env):
    env.get_result = DResp(dict(ALL_SUPPLIED, event_data=None, event_risk=True))
    analyze("TCS")
    assert "event" not in env.fetch_calls


def test_fundamental_result_is_applied_only_when_it_has_metrics(env):
    env.get_result = DResp(dict(ALL_SUPPLIED, fundamental_metrics=None))
    env.fund = ({"pe": 9}, False)
    out = analyze("TCS")
    assert out["fundamental_metrics"] == {"pe": 9} and out["fundamental_fallback"] is False
    assert env.fetch_calls == ["fund"]
    env.get_result = DResp(dict(ALL_SUPPLIED, fundamental_metrics=None))
    env.fund = ({}, True)
    out = analyze("TCS")
    assert out["fundamental_metrics"] is None and out["fundamental_fallback"] is False    # default untouched


@pytest.mark.parametrize("bad", ["none", None, {"pe": 1}])
def test_non_tuple_fundamental_result_is_ignored(env, bad):
    env.get_result = DResp(dict(ALL_SUPPLIED, fundamental_metrics=None))
    env.fund = bad
    assert analyze("TCS")["fundamental_metrics"] is None


def test_fundamental_task_exception_is_ignored(env):
    env.get_result = DResp(dict(ALL_SUPPLIED, fundamental_metrics=None))
    env.fund_exc = RuntimeError("x")
    assert analyze("TCS")["fundamental_metrics"] is None


def test_event_data_with_an_earnings_date_flags_event_risk(env):
    env.get_result = DResp(dict(ALL_SUPPLIED, event_data=None, reasons={"technical": ["t"]}))
    env.event = {"next_earnings_date": "2026-10-20", "bulk_deals": [1]}
    out = analyze("TCS")
    assert out["event_data"] == env.event and out["event_risk"] is True
    assert out["reasons"]["event"] == ["Earnings due: 2026-10-20"] and out["reasons"]["technical"] == ["t"]


def test_event_data_without_a_date_is_stored_but_does_not_flag_risk(env):
    env.get_result = DResp(dict(ALL_SUPPLIED, event_data=None))
    env.event = {"bulk_deals": [1]}
    out = analyze("TCS")
    assert out["event_data"] == {"bulk_deals": [1]} and out["event_risk"] is False and "event" not in out["reasons"]


@pytest.mark.parametrize("ev", [None, {}, ""])
def test_empty_event_result_changes_nothing(env, ev):
    env.get_result = DResp(dict(ALL_SUPPLIED, event_data=None))
    env.event = ev
    out = analyze("TCS")
    assert not out.get("event_data")


def test_event_task_exception_is_ignored(env):
    env.get_result = DResp(dict(ALL_SUPPLIED, event_data=None))
    env.event_exc = RuntimeError("x")
    assert not analyze("TCS").get("event_data")


def test_news_result_sets_score_and_reasons(env):
    env.get_result = DResp(dict(ALL_SUPPLIED, news_score=None))
    env.news = {"news_score": 72, "reasons": ["good headline"]}
    out = analyze("TCS")
    assert out["news_score"] == 72 and out["reasons"]["news"] == ["good headline"]


def test_news_result_without_reasons_only_sets_the_score(env):
    env.get_result = DResp(dict(ALL_SUPPLIED, news_score=None))
    env.news = {"news_score": 10}
    out = analyze("TCS")
    assert out["news_score"] == 10 and "news" not in out["reasons"]


def test_news_result_without_a_score_overwrites_with_none(env):
    """NOT FIXED: `normalized["news_score"] = news_data.get("news_score")` assigns None when the
    news payload has no score, discarding whatever the feed / engine had (here: nothing to lose)."""
    env.get_result = DResp(dict(ALL_SUPPLIED, news_score=None))
    env.news = {"reasons": ["r"]}
    assert analyze("TCS")["news_score"] is None


@pytest.mark.parametrize("exc", [None, RuntimeError("x")])
def test_missing_or_failed_news_leaves_the_score_unset(env, exc):
    env.get_result = DResp(dict(ALL_SUPPLIED, news_score=None))
    env.news, env.news_exc = None, exc
    assert analyze("TCS")["news_score"] is None


def test_prediction_result_sets_score_and_note(env):
    env.get_result = DResp(dict(ALL_SUPPLIED, prediction_score=None))
    env.pred = (64.0, "model says up")
    out = analyze("TCS", skip_gemini=True)
    assert out["prediction_score"] == 64.0 and out["prediction_note"] == "model says up"
    assert "🤖 model says up" in out["natural_language_summary"]


@pytest.mark.parametrize("pred", [(None, "note"), "junk", None, [1, 2]])
def test_unusable_prediction_results_are_ignored(env, pred):
    env.get_result = DResp(dict(ALL_SUPPLIED, prediction_score=None))
    env.pred = pred
    out = analyze("TCS")
    assert out["prediction_score"] is None and out["prediction_note"] is None


def test_prediction_task_exception_is_ignored(env):
    env.get_result = DResp(dict(ALL_SUPPLIED, prediction_score=None))
    env.pred_exc = RuntimeError("x")
    assert analyze("TCS")["prediction_score"] is None


def test_lite_mode_never_runs_the_enrichment_fetches(env):
    env.get_result = DResp({"decision": "HOLD", "close": 10.0})
    analyze("TCS", lite=True)
    assert env.fetch_calls == []


def test_all_four_fetches_run_when_nothing_was_supplied(env):
    env.get_result = DResp({"decision": "HOLD", "close": 10.0})
    analyze("TCS")
    assert sorted(env.fetch_calls) == ["event", "fund", "news", "pred"]


# ── holding-period estimate & summary selection ──────────────────────────────

def test_holding_period_uses_entry_low_then_close(env, monkeypatch):
    seen = []
    monkeypatch.setattr(gw, "_estimate_holding_period", lambda e, t, d: seen.append((e, t, d)) or {"est": 1})
    env.get_result = DResp(dict(ALL_SUPPLIED, entry_range={"low": 95.0, "high": 99.0}, target=120.0, decision="BUY NOW"))
    out = analyze("TCS")
    assert seen == [(95.0, 120.0, "BUY NOW")] and out["holding_period_estimate"] == {"est": 1}
    seen.clear()
    env.get_result = DResp(dict(ALL_SUPPLIED, entry_range={"low": None}, target=120.0))
    analyze("TCS")
    assert seen == [(10.0, 120.0, "HOLD")]
    seen.clear()
    env.get_result = DResp(dict(ALL_SUPPLIED, entry_range=None, target=None))
    analyze("TCS")
    assert seen == [(10.0, None, "HOLD")]


def test_real_holding_estimate_lands_on_the_row(env):
    env.get_result = DResp(dict(ALL_SUPPLIED, entry_range={"low": 100.0}, target=110.0, decision="BUY NOW"))
    est = analyze("TCS")["holding_period_estimate"]
    assert (est["min_days"], est["max_days"]) == (12, 25)


def test_single_stock_analyse_uses_the_gemini_summary(env):
    env.get_result = DResp(dict(ALL_SUPPLIED))
    assert analyze("TCS")["natural_language_summary"] == "AI summary." and env.ai_calls == ["TCS"]


@pytest.mark.parametrize("kw", [{"skip_gemini": True}, {"lite": True}])
def test_scan_modes_use_the_free_template(env, kw):
    env.get_result = DResp(dict(ALL_SUPPLIED, decision="SELL", stop_loss=9))
    out = analyze("TCS", **kw)
    assert out["natural_language_summary"].startswith("🔴 TCS") and env.ai_calls == []


def test_template_failure_leaves_the_summary_none(env, monkeypatch):
    monkeypatch.setattr(gw, "_generate_summary", lambda d: (_ for _ in ()).throw(RuntimeError("x")))
    env.get_result = DResp(dict(ALL_SUPPLIED))
    assert analyze("TCS", skip_gemini=True)["natural_language_summary"] is None


def test_engine_row_that_is_not_a_dict_is_replaced_by_the_default_row(env):
    env.get_result = DResp("garbage")
    out = analyze("TCS", skip_gemini=True)
    assert out["decision"] == "DO NOT BUY" and out["symbol"] == "TCS"
    assert env.kv_sets[0][0] == DKEY                                    # NOT FIXED: the placeholder default is cached for the TTL
