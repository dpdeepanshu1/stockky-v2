"""tests/test_main_stock_scan_routes.py — coverage for api-gateway/main.py, slice 6 (lines 4595-5300)

Pass 64. `GET /stock/{symbol}` (single-stock Analyse) and the scan start/status routes:

* `get_stock_decision` — symbol resolution, live decide call, price fill, the concurrent
  enrichment fan-out (technical / fundamental / news / events / prediction), the Neon write-back,
  the AI-summary fallback, the data-quality flags, and the three upstream-failure shapes;
* the legacy sync helpers `_merge_fundamentals`, `_fetch_news`, `_fetch_events`;
* `POST /scan`, `GET /scan`, `POST /scan/batch`, `POST /scan/start`, `GET /scan/last`,
  `GET /scan/status/{task_id}`, and `_lite_evaluate_from_feed`.

Everything downstream is faked: the per-request `httpx.AsyncClient` the handler builds itself
(one scripted `FakeClient`), the shared async client, the sync `httpx.get`, the kv cache
(`_redis_get` / `_redis_set` -> a dict), Redis, `run_scan_parallel`, `_analyze_one_symbol_ultra`,
and the data-feed write-back. Nothing touches the network or a database. Findings are pinned as
current behaviour and marked ``NOT FIXED``. The ones fixed afterwards (non-JSON decide reply, blank symbols
and the cap, bare-string / null /scan/batch bodies, /scan/last partial rule, negative ETA, the degraded-HOLD scores and the
`background_tasks=None` scan fallbacks) now pin the fixed behaviour; the universe-cache pin turned out to be a stale test artefact (see TestStockRedisClear).

Run from services/api-gateway:
    python3 -m pytest tests/test_main_stock_scan_routes.py -v
"""
from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace

import httpx
import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)

from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient


def _run(coro):
    return asyncio.run(coro)


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status=200, data=None, content=True, json_raises=False, text=""):
        self.status_code = status
        self._data = {} if data is None else data
        self.content = b"x" if content else b""
        self.text = text
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("bad json")
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)


class FakeClient:
    """Scripted async client. `routes` maps a URL substring -> FakeResp | Exception | callable."""

    def __init__(self):
        self.routes = {}
        self.calls = []            # (method, url, kwargs)

    async def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        for frag, out in self.routes.items():
            if frag in url:
                if callable(out):
                    out = out(url, kw)
                if isinstance(out, Exception):
                    raise out
                return out
        raise httpx.ConnectError("no route for " + url)

    def urls(self):
        return [u for _, u, _ in self.calls]


class _CM:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *a):
        return False


class FakeRedis:
    def __init__(self):
        self.deleted = []
        self.raise_on_delete = False

    def delete(self, key):
        if self.raise_on_delete:
            raise RuntimeError("redis down")
        self.deleted.append(key)


class KV:
    """Dict-backed stand-in for the gateway's `_redis_get` / `_redis_set`."""

    def __init__(self):
        self.store = {}
        self.sets = []          # (key, value, ttl)

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl=None):
        self.sets.append((key, value, ttl))
        self.store[key] = value


@pytest.fixture
def kv(monkeypatch):
    k = KV()
    monkeypatch.setattr(gw, "_redis_get", k.get)
    monkeypatch.setattr(gw, "_redis_set", k.set)
    return k


@pytest.fixture
def rds(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(gw, "_redis", r)
    return r


@pytest.fixture
def tc():
    return TestClient(gw.app, raise_server_exceptions=False)


@pytest.fixture
def logs(monkeypatch):
    rec = {"warning": [], "debug": [], "info": [], "error": []}

    class L:
        def _f(self, bucket, msg, a):
            rec[bucket].append(msg % a if a else msg)

        def warning(self, msg, *a, **k):
            self._f("warning", msg, a)

        def debug(self, msg, *a, **k):
            self._f("debug", msg, a)

        def info(self, msg, *a, **k):
            self._f("info", msg, a)

        def error(self, msg, *a, **k):
            self._f("error", msg, a)

        def exception(self, msg, *a, **k):
            self._f("error", msg, a)

    monkeypatch.setattr(gw, "logger", L())
    return rec


# ── /stock/{symbol} ──────────────────────────────────────────────────────────

D = lambda s: f"{gw.DECISION_URL}/decide/{s}"            # noqa: E731
T = lambda s: f"{gw.TECHNICAL_URL}/analyze/{s}"          # noqa: E731
F = lambda s: f"{gw.FUNDAMENTAL_URL}/analyze/{s}"        # noqa: E731
N = lambda s: f"{gw.NEWS_URL}/analyze/{s}"               # noqa: E731
E = lambda s: f"{gw.EVENT_URL}/events/{s}"               # noqa: E731
P = lambda s: f"{gw.PREDICTION_URL}/predict/{s}"         # noqa: E731


def _full_decide(**over):
    """A decide payload with every pillar present -> no enrichment is triggered."""
    raw = {
        "decision": "BUY", "confidence": "High", "combined_score": 72,
        "technical_score": 70, "fundamental_score": 68, "news_score": 60,
        "prediction_score": 64, "market_score": 55, "training_score": 62,
        "close": 100.0, "support": 95.0, "resistance": 110.0,
        "fundamental_metrics": {"pe": 20}, "event_data": {"x": 1},
        "reasons": {"technical": ["ok"], "fundamental": ["ok"], "news": ["ok"]},
    }
    raw.update(over)
    return raw


class Env:
    def __init__(self, monkeypatch):
        self.mp = monkeypatch
        self.client = FakeClient()
        self.searched = []
        self.known = {"TCS", "INFY", "RELIANCE"}
        self.resolved = "SAME"          # "SAME" -> echo the symbol upper-cased; None -> None; str -> that
        self.price = 123.45
        self.price_exc = None
        self.summary_exc = None
        self.saved = []
        self.extract_result = {"row": 1}
        self.extract_exc = None
        self.save_exc = None
        self.client_timeouts = []

        def _factory(*a, **k):
            self.client_timeouts.append(k.get("timeout"))
            return _CM(self.client)

        monkeypatch.setattr(gw.httpx, "AsyncClient", _factory)
        monkeypatch.setattr(gw, "_resolve_symbol", self._resolve)
        monkeypatch.setattr(gw, "_add_searched", self.searched.append)
        monkeypatch.setattr(gw, "_get_all_known_symbols", lambda: self.known)
        monkeypatch.setattr(gw, "_fetch_price_from_quote", self._price)
        monkeypatch.setattr(gw, "_generate_ai_summary", self._ai)
        monkeypatch.setattr(gw, "_generate_summary", lambda r: "TEMPLATE-SUMMARY")

        import data_feed
        monkeypatch.setattr(data_feed, "extract_feed_payload", self._extract)
        monkeypatch.setattr(data_feed, "save_stock_feed", self._save)

    # stubs
    def _resolve(self, original):
        if self.resolved == "SAME":
            return original.upper()
        return self.resolved

    def _price(self, sym):
        if self.price_exc:
            raise self.price_exc
        return self.price

    async def _ai(self, result, client):
        if self.summary_exc:
            raise self.summary_exc
        return "AI-SUMMARY"

    def _extract(self, sym, **kw):
        if self.extract_exc:
            raise self.extract_exc
        self.last_extract = (sym, kw)
        return self.extract_result

    def _save(self, sym, row):
        if self.save_exc:
            raise self.save_exc
        self.saved.append((sym, row))

    # helpers
    def decide(self, sym, raw=None, status=200):
        self.client.routes[D(sym)] = FakeResp(status, _full_decide() if raw is None else raw)

    def passthrough(self):
        self.mp.setattr(gw, "_normalize_decision_response", lambda raw, sym: dict(raw))


@pytest.fixture
def env(monkeypatch):
    return Env(monkeypatch)


def stock(symbol="TCS", **kw):
    return _run(gw.get_stock_decision(symbol, **kw))


class TestStockHappyPath:
    def test_complete_decide_needs_no_enrichment(self, env):
        env.decide("TCS")
        out = stock("tcs")
        assert out["symbol"] == "TCS" and out["decision"] == "BUY"
        assert out["enrichment"] == {"need_tech": False, "need_fund": False, "need_news": False,
                                     "need_events": False, "need_pred": False, "fetched": []}
        assert env.client.urls() == [D("TCS")]
        assert out["natural_language_summary"] == "AI-SUMMARY"
        assert out["data_quality"] == {"level": "high", "flags": [], "note": "Core inputs present"}
        assert "corrected_from" not in out

    def test_decide_is_called_live_with_force_and_already_owned(self, env):
        env.decide("TCS")
        stock("TCS", already_owned=True)
        _, url, kw = env.client.calls[0]
        assert kw["params"] == {"already_owned": "true", "force": "true"}
        stock("TCS")
        assert env.client.calls[1][2]["params"]["already_owned"] == "false"

    def test_handler_builds_its_own_client_with_the_configured_decide_timeout(self, env):
        # FIXED (group 81): was a hard 20 s read timeout, which the first analysis after a boot
        # (cold caches) overran. Now STOCK_DECIDE_TIMEOUT_SEC (default 60 s).
        env.decide("TCS")
        stock("TCS")
        t = env.client_timeouts[0]
        assert t.read == gw.STOCK_DECIDE_TIMEOUT_SEC == 60.0 and t.connect == 5.0

    def test_symbol_is_stripped_and_recorded_as_searched(self, env):
        env.decide("TCS")
        stock("  tcs  ")
        assert env.searched == ["TCS"]

    def test_served_over_http(self, env, tc):
        env.decide("TCS")
        r = tc.get("/stock/tcs?already_owned=true")
        assert r.status_code == 200 and r.json()["symbol"] == "TCS"
        assert env.client.calls[0][2]["params"]["already_owned"] == "true"


class TestStockSymbolResolution:
    def test_close_match_is_corrected_and_reported(self, env):
        env.resolved = "TATAMOTORS"
        env.decide("TATAMOTORS")
        out = stock("tatamotor")
        assert out["corrected_from"] == "TATAMOTOR" and out["symbol"] == "TATAMOTORS"
        assert env.searched == ["TATAMOTORS"]
        assert env.client.urls()[0] == D("TATAMOTORS")

    def test_unresolvable_symbol_is_used_as_typed_uppercased(self, env):
        env.resolved = None
        env.decide("ZZZ")
        out = stock("zzz")
        assert out["symbol"] == "ZZZ" and "corrected_from" not in out

    def test_resolved_equal_to_input_is_not_a_correction(self, env):
        env.resolved = "TCS"
        env.decide("TCS")
        assert "corrected_from" not in stock("TCS")


class TestStockRedisClear:
    def test_universe_cache_key_is_deleted_when_redis_is_on(self, env, rds):
        env.decide("TCS")
        stock("TCS")
        assert rds.deleted == [gw.SCAN_UNIVERSE_KEY]

    def test_redis_failure_is_swallowed(self, env, rds):
        rds.raise_on_delete = True
        env.decide("TCS")
        assert stock("TCS")["symbol"] == "TCS"

    def test_redis_off_still_clears_the_universe_from_the_real_cache_layers(self, env, monkeypatch):
        """RESOLVED (the old pin was a stale test artefact): main.py already clears the kv / in-process /
        Redis layers through `_drop_cache_keys`, and DELETE /scan/universe/cache does the same. The old test
        used a fake kv dict that those code paths can never touch, so it only looked broken. This one uses the
        real `_redis_set` / `_redis_get` with Upstash and Neon off (the default)."""
        monkeypatch.setattr(gw, "_redis", None)
        monkeypatch.setattr(gw, "_kv_cache", None)
        monkeypatch.setattr(gw, "_mem_kv", {})
        monkeypatch.setattr(gw, "_mem_kv_exp", {})
        gw._redis_set(gw.SCAN_UNIVERSE_KEY, ["AAA", "BBB"], ttl=300)
        assert gw._redis_get(gw.SCAN_UNIVERSE_KEY) == ["AAA", "BBB"]
        env.decide("TCS")
        stock("TCS")
        assert gw._redis_get(gw.SCAN_UNIVERSE_KEY) is None

    def test_delete_universe_cache_route_clears_the_real_cache_layers(self, monkeypatch):
        monkeypatch.setattr(gw, "_redis", None)
        monkeypatch.setattr(gw, "_kv_cache", None)
        monkeypatch.setattr(gw, "_mem_kv", {})
        monkeypatch.setattr(gw, "_mem_kv_exp", {})
        gw._redis_set(gw.SCAN_UNIVERSE_KEY, ["AAA", "BBB"], ttl=300)
        out = gw.clear_universe_cache()
        assert out["message"].startswith("Scan universe cache cleared") and out["cleared"] == ["memory"]
        assert gw._redis_get(gw.SCAN_UNIVERSE_KEY) is None


class TestStockPriceFill:
    def test_missing_close_is_filled_from_quote_and_derives_support_resistance(self, env):
        env.passthrough()
        env.decide("TCS", {"decision": "BUY", "close": None, "data_insufficient": True,
                           "technical_score": 50, "fundamental_score": 50, "reasons": {}})
        out = stock("TCS")
        assert out["close"] == 123.45 and out["data_insufficient"] is False
        assert out["support"] == round(123.45 * 0.97, 2)
        assert out["resistance"] == round(123.45 * 1.03, 2)

    def test_existing_support_and_resistance_are_kept(self, env):
        env.passthrough()
        env.decide("TCS", {"close": 100, "support": 90, "resistance": 120, "reasons": {}})
        out = stock("TCS")
        assert out["support"] == 90 and out["resistance"] == 120

    def test_quote_failure_is_swallowed_and_stays_insufficient(self, env):
        env.passthrough()
        env.price_exc = RuntimeError("quote down")
        env.decide("TCS", {"close": None, "data_insufficient": True, "reasons": {}})
        out = stock("TCS")
        assert out["close"] is None and out["data_insufficient"] is True

    def test_quote_returning_none_leaves_close_unset(self, env):
        env.passthrough()
        env.price = None
        env.decide("TCS", {"close": None, "reasons": {}})
        assert stock("TCS")["close"] is None

    def test_non_numeric_close_skips_derivation_but_clears_insufficient(self, env):
        env.passthrough()
        env.decide("TCS", {"close": "n/a", "data_insufficient": True, "reasons": {}})
        out = stock("TCS")
        assert out["data_insufficient"] is False
        assert out.get("support") is None and out.get("resistance") is None


class TestStockEnrichment:
    def _bare(self, env, **over):
        raw = {"decision": "HOLD", "close": 100.0, "support": 90.0, "resistance": 110.0,
               "technical_score": 50, "fundamental_score": 50, "reasons": {}}
        raw.update(over)
        env.passthrough()
        env.decide("TCS", raw)

    def test_neutral_score_50_alone_triggers_nothing_but_the_defaults(self, env):
        # news/event/prediction have no data -> those three fetch; tech/fund (50, no blob) do not
        self._bare(env)
        out = stock("TCS")
        assert out["enrichment"]["need_tech"] is False and out["enrichment"]["need_fund"] is False
        assert out["enrichment"]["need_news"] is True
        assert out["enrichment"]["need_events"] is True
        assert out["enrichment"]["need_pred"] is True
        urls = env.client.urls()
        assert N("TCS") in urls and E("TCS") + "?force=true" in urls and P("TCS") in urls
        assert T("TCS") + "?force=true" not in urls

    @pytest.mark.parametrize("phrase", [
        "temporarily unavailable", "error processing", "recovering", "unavailable", "timed out",
        "timeout", "failed to", "not available", "data insufficient", "could not",
    ])
    def test_every_failure_phrase_marks_a_pillar_failed(self, env, phrase):
        self._bare(env, reasons={"technical": [f"RSI {phrase.upper()} now"]})
        env.client.routes[T("TCS")] = FakeResp(200, {"technical_score": 77})
        out = stock("TCS")
        assert out["enrichment"]["need_tech"] is True and out["technical_score"] == 77

    def test_technical_enrichment_applies_score_fills_gaps_and_replaces_reasons(self, env):
        self._bare(env, close=None, support=None, reasons={"technical": ["data unavailable"]},
                   data_insufficient=False)
        env.price = None     # keep close unset so the tech payload can fill it
        env.client.routes[T("TCS")] = FakeResp(200, {
            "technical_score": 81, "support": 88.0, "resistance": 120.0, "trend_strength": 0.7,
            "volume_surge": 1.4, "rsi": 61, "close": 101.0, "reasons": "strong trend"})
        out = stock("TCS")
        assert out["technical_score"] == 81
        assert out["support"] == 88.0 and out["close"] == 101.0 and out["rsi"] == 61
        assert out["reasons"]["technical"] == ["strong trend"]      # str wrapped into a list
        assert "tech" in out["enrichment"]["fetched"]

    def test_technical_enrichment_never_overwrites_values_decide_already_has(self, env):
        self._bare(env, reasons={"technical": ["unavailable"]}, rsi=55)
        env.client.routes[T("TCS")] = FakeResp(200, {"technical_score": None, "rsi": 99,
                                                     "support": 1.0, "reasons": ["a", "b"]})
        out = stock("TCS")
        assert out["technical_score"] == 50 and out["rsi"] == 55 and out["support"] == 90.0
        assert out["reasons"]["technical"] == ["a", "b"]

    def test_tech_needed_when_score_missing_and_data_insufficient(self, env):
        self._bare(env, technical_score=None, data_insufficient=True, close=None)
        env.price = None
        out = stock("TCS")
        assert out["enrichment"]["need_tech"] is True

    def test_fundamental_enrichment_writes_back_to_the_feed(self, env):
        self._bare(env, fundamental_score=None, fundamental_metrics=None)
        env.client.routes[F("TCS")] = FakeResp(200, {
            "fundamental_score": 66, "metrics": {"roce": 22}, "fallback_used": True,
            "reasons": ["solid"]})
        out = stock("TCS")
        assert out["fundamental_score"] == 66 and out["fundamental_metrics"] == {"roce": 22}
        assert out["fundamental_fallback"] is True and out["reasons"]["fundamental"] == ["solid"]
        assert env.saved == [("TCS", {"row": 1})]
        sym, kw = env.last_extract
        assert sym == "TCS" and kw["extra"] == {"fundamental_score": 66, "source": "stock_enrich_fundamental"}
        assert env.client.calls[[c[1] for c in env.client.calls].index(F("TCS") + "?force=true")][2]["timeout"] == 60.0

    def test_fundamental_without_metrics_keeps_existing_and_clears_fallback_flag(self, env):
        self._bare(env, fundamental_score=None, fundamental_metrics=None)
        env.client.routes[F("TCS")] = FakeResp(200, {"fundamental_score": None, "fallback_used": False,
                                                     "reasons": ["r1", "r2"]})
        out = stock("TCS")
        assert out["fundamental_fallback"] is False and out["fundamental_metrics"] is None
        assert out["fundamental_score"] is None

    def test_fundamental_writeback_failure_is_swallowed(self, env, logs):
        self._bare(env, fundamental_score=None, fundamental_metrics=None)
        env.client.routes[F("TCS")] = FakeResp(200, {"fundamental_score": 66})
        env.save_exc = RuntimeError("neon down")
        assert stock("TCS")["fundamental_score"] == 66
        assert any("fund writeback TCS" in m for m in logs["debug"])

    def test_empty_feed_row_is_not_saved(self, env):
        self._bare(env, fundamental_score=None, fundamental_metrics=None)
        env.client.routes[F("TCS")] = FakeResp(200, {"fundamental_score": 66})
        env.extract_result = None
        stock("TCS")
        assert env.saved == []

    def test_fundamental_needed_on_failure_blob_even_with_score(self, env):
        self._bare(env, fundamental_score=55, reasons={"fundamental": ["Could not load ratios"]})
        env.client.routes[F("TCS")] = FakeResp(200, {"fundamental_score": 70})
        out = stock("TCS")
        assert out["enrichment"]["need_fund"] is True and out["fundamental_score"] == 70

    def test_news_enrichment(self, env):
        self._bare(env, news_score=None, reasons={})
        env.client.routes[N("TCS")] = FakeResp(200, {"news_score": 58, "reasons": "upbeat"})
        out = stock("TCS")
        assert out["news_score"] == 58 and out["reasons"]["news"] == ["upbeat"]
        assert env.client.calls[[c[1] for c in env.client.calls].index(N("TCS"))][2]["timeout"] == 30.0

    def test_news_payload_without_a_score_is_ignored(self, env):
        self._bare(env, news_score=None, reasons={})
        env.client.routes[N("TCS")] = FakeResp(200, {"news_score": None, "reasons": ["x"]})
        out = stock("TCS")
        assert out["news_score"] is None and "news" not in out["reasons"]

    def test_news_not_fetched_when_reasons_exist_and_are_not_failures(self, env):
        self._bare(env, news_score=None, reasons={"news": ["quiet week"]})
        assert stock("TCS")["enrichment"]["need_news"] is False

    def test_news_fetched_when_its_reasons_report_a_failure(self, env):
        self._bare(env, news_score=None, reasons={"news": ["feed timed out"]})
        assert stock("TCS")["enrichment"]["need_news"] is True

    def test_event_enrichment_appends_earnings_reason(self, env):
        self._bare(env, event_data=None)
        env.client.routes[E("TCS")] = FakeResp(200, {"next_earnings_date": "2026-10-15"})
        out = stock("TCS")
        assert out["event_data"] == {"next_earnings_date": "2026-10-15"}
        assert out["event_risk"] is True
        assert out["reasons"]["event"] == ["Earnings due: 2026-10-15"]

    def test_event_enrichment_without_earnings_leaves_risk_alone(self, env):
        self._bare(env, event_data=None)
        env.client.routes[E("TCS")] = FakeResp(200, {"note": "none"})
        out = stock("TCS")
        assert out["event_data"] == {"note": "none"} and not out.get("event_risk")
        assert "event" not in out["reasons"]

    def test_event_reasons_that_are_not_a_list_are_left_untouched(self, env):
        self._bare(env, event_data=None, event_risk=False, reasons={"event": "str-not-list"})
        # a truthy reasons["event"] suppresses the fetch
        assert stock("TCS")["enrichment"]["need_events"] is False

    def test_event_risk_true_suppresses_the_events_fetch(self, env):
        self._bare(env, event_data=None, event_risk=True)
        assert stock("TCS")["enrichment"]["need_events"] is False

    def test_prediction_enrichment_needs_model_loaded(self, env):
        self._bare(env, prediction_score=None, prediction_note=None)
        env.client.routes[P("TCS")] = FakeResp(200, {"model_loaded": True, "prediction_score": 73,
                                                     "note": "calibrated"})
        out = stock("TCS")
        assert out["prediction_score"] == 73 and out["prediction_note"] == "calibrated"
        assert env.client.calls[[c[1] for c in env.client.calls].index(P("TCS"))][2]["timeout"] == 30.0

    def test_prediction_from_an_unloaded_model_is_ignored(self, env):
        self._bare(env, prediction_score=None, prediction_note=None)
        env.client.routes[P("TCS")] = FakeResp(200, {"model_loaded": False, "prediction_score": 73})
        assert stock("TCS")["prediction_score"] is None

    def test_prediction_with_loaded_model_but_no_score_or_note(self, env):
        self._bare(env, prediction_score=None, prediction_note=None)
        env.client.routes[P("TCS")] = FakeResp(200, {"model_loaded": True})
        out = stock("TCS")
        assert out["prediction_score"] is None and out["prediction_note"] is None

    def test_a_prediction_note_alone_suppresses_the_prediction_fetch(self, env):
        self._bare(env, prediction_score=None, prediction_note="model cold")
        assert stock("TCS")["enrichment"]["need_pred"] is False

    def test_all_five_enrichments_run_in_one_call_and_report_fetched(self, env):
        self._bare(env, technical_score=None, data_insufficient=True, close=None,
                   fundamental_score=None, news_score=None, event_data=None,
                   prediction_score=None, prediction_note=None)
        env.price = None
        env.client.routes[T("TCS")] = FakeResp(200, {"technical_score": 60})
        env.client.routes[F("TCS")] = FakeResp(200, {"fundamental_score": 61})
        env.client.routes[N("TCS")] = FakeResp(200, {"news_score": 62})
        env.client.routes[E("TCS")] = FakeResp(200, {"k": 1})
        env.client.routes[P("TCS")] = FakeResp(200, {"model_loaded": True, "prediction_score": 63})
        out = stock("TCS")
        assert sorted(out["enrichment"]["fetched"]) == ["events", "fund", "news", "pred", "tech"]
        assert all(out["enrichment"][k] for k in
                   ("need_tech", "need_fund", "need_news", "need_events", "need_pred"))
        assert (out["technical_score"], out["fundamental_score"], out["news_score"],
                out["prediction_score"]) == (60, 61, 62, 63)

    @pytest.mark.parametrize("resp", [
        FakeResp(503), FakeResp(200, content=False), FakeResp(200, ["not", "a", "dict"]),
        FakeResp(200, json_raises=True), httpx.ReadTimeout("slow"),
    ])
    def test_a_failed_enrichment_fetch_degrades_to_nothing(self, env, resp, logs):
        self._bare(env, news_score=None)
        env.client.routes[N("TCS")] = resp
        out = stock("TCS")
        assert out["news_score"] is None and "news" not in out["enrichment"]["fetched"]

    def test_enrichment_exception_is_logged_at_debug(self, env, logs):
        self._bare(env, news_score=None)
        env.client.routes[N("TCS")] = httpx.ReadTimeout("slow")
        stock("TCS")
        assert any("enrich" in m for m in logs["debug"])

    def test_an_exception_escaping_an_enrichment_task_is_recorded_as_none(self, env, logs):
        # Defensive branch: `_get` swallows Exception itself, so a task only ends with an exception
        # if it dies another way (a BaseException that is not CancelledError).
        self._bare(env, news_score=None)

        class Boom(BaseException):
            pass

        def _die(url, kw):
            raise Boom("boom")

        env.client.routes[N("TCS")] = _die
        out = stock("TCS")
        assert out["enrichment"]["fetched"] == []
        assert any("enrich task" in m and "boom" in m for m in logs["debug"])

    def test_a_non_dict_reasons_payload_is_replaced_by_a_dict(self, env):
        # The `if not isinstance(reasons, dict)` re-assignment right after the isinstance guard
        # is dead code; the end-of-function `result["reasons"] = reasons` is what fixes it up.
        self._bare(env, reasons=["oops", "list"])
        out = stock("TCS")
        assert isinstance(out["reasons"], dict)


class TestStockSummaryAndQuality:
    def test_ai_summary_failure_falls_back_to_the_template(self, env):
        env.decide("TCS")
        env.summary_exc = RuntimeError("gemini down")
        assert stock("TCS")["natural_language_summary"] == "TEMPLATE-SUMMARY"

    def _q(self, env, **over):
        env.passthrough()
        raw = _full_decide(**over)
        env.decide("TCS", raw)
        return stock("TCS")["data_quality"]

    def test_fundamental_fallback_is_medium(self, env):
        dq = self._q(env, fundamental_fallback=True)
        assert dq["level"] == "medium" and dq["flags"] == ["Fundamentals partial/fallback"]
        assert dq["note"] == "Scores may be soft — limited free data"

    def test_data_insufficient_is_medium(self, env):
        # the quote fill would otherwise supply a close and clear data_insufficient
        env.price = None
        dq = self._q(env, data_insufficient=True, close=None)
        assert dq["level"] == "medium" and "Fundamentals partial/fallback" in dq["flags"]

    def test_missing_news_alone_is_medium(self, env):
        env.client.routes[N("TCS")] = FakeResp(503)
        dq = self._q(env, news_score=None, reasons={"technical": ["ok"], "news": ["x"]})
        assert dq["level"] == "medium" and dq["flags"] == ["News unavailable"]

    def test_fallback_plus_missing_news_drops_to_low(self, env):
        env.client.routes[N("TCS")] = FakeResp(503)
        dq = self._q(env, fundamental_fallback=True, news_score=None,
                     reasons={"technical": ["ok"], "news": ["x"]})
        assert dq["level"] == "low"
        assert dq["flags"] == ["Fundamentals partial/fallback", "News unavailable"]

    def test_missing_model_score_adds_a_flag_without_lowering_the_level(self, env):
        env.client.routes[P("TCS")] = FakeResp(503)
        dq = self._q(env, prediction_score=None)
        assert dq["flags"] == ["Model score missing"] and dq["level"] == "high"
        assert dq["note"] == "Scores may be soft — limited free data"

    def test_unofficial_delivery_pct_is_medium(self, env):
        dq = self._q(env, reasons={"technical": ["Delivery % data unavailable"], "news": ["ok"]})
        assert dq["level"] == "medium" and dq["flags"] == ["Delivery % not official"]

    @pytest.mark.parametrize("ts", [None, 0, 50])
    def test_thin_training_signal_flag(self, env, ts):
        dq = self._q(env, training_score=ts)
        assert dq["flags"] == ["Training signal thin"] and dq["level"] == "high"


class TestStockUpstreamFailures:
    def test_decide_404_suggests_close_known_symbols(self, env):
        env.known = {"INFY", "INFOSYS", "TCS"}
        env.client.routes[D("INFX")] = FakeResp(404)
        env.resolved = None
        with pytest.raises(HTTPException) as e:
            stock("infx")
        assert e.value.status_code == 404
        assert e.value.detail.startswith("Symbol 'INFX' not found. Did you mean: ")
        assert "INFY" in e.value.detail

    def test_decide_404_with_no_close_matches(self, env):
        env.known = {"TCS"}
        env.client.routes[D("QQQQQ")] = FakeResp(404)
        env.resolved = None
        with pytest.raises(HTTPException) as e:
            stock("qqqqq")
        assert e.value.detail == "Symbol 'QQQQQ' not found."

    def test_decide_404_over_http(self, env, tc):
        env.known = set()
        env.resolved = None
        env.client.routes[D("NOPE")] = FakeResp(404)
        r = tc.get("/stock/nope")
        assert r.status_code == 404 and "not found" in r.json()["detail"]

    def test_decide_5xx_degrades_to_a_neutral_hold(self, env, logs):
        env.client.routes[D("TCS")] = FakeResp(500, text="oom " * 100)
        out = stock("TCS")
        assert out["ok"] is True and out["decision"] == "HOLD" and out["data_insufficient"] is True
        assert out["error"] == "decision engine returned HTTP 500"
        assert "temporarily unavailable (HTTP 500)" in out["natural_language_summary"]
        assert out["data_quality"]["level"] == "low"
        assert out["data_quality"]["flags"] == ["Decision engine error"]
        assert any("decision engine HTTP 500 for TCS" in m for m in logs["warning"])

    @pytest.mark.parametrize("failure", [
        FakeResp(502), FakeResp(200, json_raises=True), httpx.ConnectError("refused"),
    ])
    def test_the_degraded_hold_carries_neutral_scores_and_price(self, env, failure):
        # FIXED: the degraded payload used to omit close/scores entirely, so a UI reading
        # `technical_score` etc. saw undefined. All three degraded paths (HTTP error, non-JSON 200,
        # unreachable) now carry the same neutral fields as the normal path, still flagged
        # data_insufficient / low quality so it is never mistaken for a real analysis.
        env.client.routes[D("TCS")] = failure
        out = stock("TCS")
        assert out["decision"] == "HOLD" and out["data_insufficient"] is True
        assert out["data_quality"]["level"] == "low"
        assert out["technical_score"] == 50 and out["fundamental_score"] == 50
        assert out["market_score"] == 50 and out["training_score"] == 50 and out["combined_score"] == 0
        assert out["news_score"] is None and out["prediction_score"] is None
        assert out["close"] is None and out["entry_range"] is None
        assert out["target"] is None and out["stop_loss"] is None
        assert out["confidence"] == "Low" and out["holding_period"] == "N/A"
        assert out["reasons"] == {"technical": ["Data unavailable"], "fundamental": ["Data unavailable"]}
        # same key set on every degraded path (one shared builder)
        assert set(out) == set(gw._degraded_hold("X", "e", "s", "f", "n"))

    @pytest.mark.parametrize("exc", [httpx.ConnectError("refused")])
    def test_unreachable_decision_service_degrades_to_hold(self, env, exc, logs):
        env.client.routes[D("TCS")] = exc
        out = stock("TCS")
        assert out["decision"] == "HOLD" and out["error"].startswith("decision engine unreachable: ")
        assert "unreachable right now" in out["natural_language_summary"]
        assert out["data_quality"]["flags"] == ["Decision engine unreachable"]
        assert any("decision engine unreachable for TCS" in m for m in logs["warning"])

    def test_a_non_json_200_from_decide_degrades_to_hold(self, env, tc, logs):
        # FIXED: only httpx errors were caught, so a 200 whose body is not JSON escaped as a bare 500.
        env.client.routes[D("TCS")] = FakeResp(200, json_raises=True)
        out = stock("TCS")
        assert out["decision"] == "HOLD" and out["data_insufficient"] is True
        assert out["error"] == "decision engine returned a non-JSON response"
        assert out["data_quality"]["flags"] == ["Decision engine error"]
        assert any("returned non-JSON for TCS" in m for m in logs["warning"])
        assert tc.get("/stock/tcs").status_code == 200


# ── legacy sync fallback helpers ─────────────────────────────────────────────

class TestLegacyHelpers:
    @pytest.fixture
    def sync_get(self, monkeypatch):
        st = SimpleNamespace(resp=FakeResp(200, {}), exc=None, urls=[], kw=[])

        def _get(url, **kw):
            st.urls.append(url)
            st.kw.append(kw)
            if st.exc:
                raise st.exc
            return st.resp

        monkeypatch.setattr(gw.httpx, "get", _get)
        return st

    # _merge_fundamentals
    def test_merge_fundamentals_cache_hit_skips_http(self, kv, sync_get):
        kv.store[f"{gw.FUNDAMENTAL_CACHE_PREFIX}TCS"] = {"metrics": {"pe": 1}, "fallback": True}
        n = {}
        assert gw._merge_fundamentals(n, "TCS") is None
        assert n == {"fundamental_metrics": {"pe": 1}, "fundamental_fallback": True}
        assert sync_get.urls == []

    def test_merge_fundamentals_cache_without_metrics_refetches(self, kv, sync_get):
        kv.store[f"{gw.FUNDAMENTAL_CACHE_PREFIX}TCS"] = {"metrics": None}
        sync_get.resp = FakeResp(200, {"metrics": {"roe": 9}, "fallback_used": False})
        n = {}
        gw._merge_fundamentals(n, "TCS")
        assert n == {"fundamental_metrics": {"roe": 9}, "fundamental_fallback": False}
        assert sync_get.urls == [f"{gw.FUNDAMENTAL_URL}/analyze/TCS"] and sync_get.kw[0]["timeout"] == 60
        assert kv.sets[-1] == (f"{gw.FUNDAMENTAL_CACHE_PREFIX}TCS",
                               {"metrics": {"roe": 9}, "fallback": False}, gw.STATIC_PARAM_TTL)

    def test_merge_fundamentals_empty_metrics_become_an_empty_dict(self, kv, sync_get):
        sync_get.resp = FakeResp(200, {"fallback_used": True})
        n = {}
        gw._merge_fundamentals(n, "TCS")
        assert n == {"fundamental_metrics": {}, "fundamental_fallback": True}

    def test_merge_fundamentals_non_dict_cache_entry_is_ignored(self, kv, sync_get):
        kv.store[f"{gw.FUNDAMENTAL_CACHE_PREFIX}TCS"] = "garbage"
        sync_get.resp = FakeResp(200, {"metrics": {"a": 1}})
        n = {}
        gw._merge_fundamentals(n, "TCS")
        assert n["fundamental_metrics"] == {"a": 1}

    def test_merge_fundamentals_non_200_leaves_input_untouched(self, kv, sync_get):
        sync_get.resp = FakeResp(503)
        n = {"keep": 1}
        gw._merge_fundamentals(n, "TCS")
        assert n == {"keep": 1} and kv.sets == []

    def test_merge_fundamentals_exception_is_logged(self, kv, sync_get, logs):
        sync_get.exc = httpx.ConnectError("")
        n = {}
        gw._merge_fundamentals(n, "TCS")
        assert n == {} and any("Fundamental fetch failed for TCS: ConnectError" in m
                               for m in logs["warning"])

    def test_merge_fundamentals_list_body_is_caught(self, kv, sync_get, logs):
        sync_get.resp = FakeResp(200, [1, 2])
        n = {}
        gw._merge_fundamentals(n, "TCS")
        assert n == {} and logs["warning"]

    # _fetch_news
    def test_fetch_news_cache_hit(self, kv, sync_get):
        kv.store[f"{gw.NEWS_CACHE_PREFIX}TCS"] = {"news_score": 5}
        assert gw._fetch_news("TCS") == {"news_score": 5} and sync_get.urls == []

    @pytest.mark.parametrize("open_,ttl", [(True, 3600), (False, None)])
    def test_fetch_news_caches_with_a_market_hours_ttl(self, kv, sync_get, monkeypatch, open_, ttl):
        monkeypatch.setattr(gw, "_is_market_open_ist", lambda: open_)
        sync_get.resp = FakeResp(200, {"news_score": 7})
        assert gw._fetch_news("TCS") == {"news_score": 7}
        assert kv.sets[-1] == (f"{gw.NEWS_CACHE_PREFIX}TCS", {"news_score": 7},
                               ttl or gw.STATIC_PARAM_TTL)
        assert sync_get.urls == [f"{gw.NEWS_URL}/analyze/TCS"] and sync_get.kw[0]["timeout"] == 30

    @pytest.mark.parametrize("body", [{}, [], None])
    def test_fetch_news_empty_or_non_dict_body_is_none_and_uncached(self, kv, sync_get, body):
        sync_get.resp = FakeResp(200, body if body is not None else {})
        if body is None:
            sync_get.resp._data = None
        assert gw._fetch_news("TCS") is None and kv.sets == []

    def test_fetch_news_non_200_and_exception(self, kv, sync_get, logs):
        sync_get.resp = FakeResp(500)
        assert gw._fetch_news("TCS") is None
        sync_get.exc = httpx.ReadTimeout("")
        assert gw._fetch_news("TCS") is None
        assert any("News fetch failed for TCS: ReadTimeout" in m for m in logs["warning"])

    # _fetch_events
    def test_fetch_events_cache_hit_and_miss(self, kv, sync_get):
        kv.store[f"{gw.EVENT_CACHE_PREFIX}TCS"] = {"e": 1}
        assert gw._fetch_events("TCS") == {"e": 1} and sync_get.urls == []
        kv.store.clear()
        sync_get.resp = FakeResp(200, {"e": 2})
        assert gw._fetch_events("TCS") == {"e": 2}
        assert sync_get.urls == [f"{gw.EVENT_URL}/events/TCS"] and sync_get.kw[0]["timeout"] == 60
        assert kv.sets[-1] == (f"{gw.EVENT_CACHE_PREFIX}TCS", {"e": 2}, gw.STATIC_PARAM_TTL)

    def test_fetch_events_failures(self, kv, sync_get, logs):
        sync_get.resp = FakeResp(200, {})
        assert gw._fetch_events("TCS") is None
        sync_get.resp = FakeResp(404)
        assert gw._fetch_events("TCS") is None
        sync_get.exc = httpx.ConnectError("")
        assert gw._fetch_events("TCS") is None
        assert kv.sets == []
        assert any("Events fetch failed for TCS" in m for m in logs["warning"])


# ── POST /scan, GET /scan ────────────────────────────────────────────────────

def _cached_scan(processed=300, total=300, result=None, **extra):
    res = {"scanned": processed, "universe_size": total, "recommendations": [{"symbol": "AAA"}]}
    if result is not None:
        res = result
    d = {"task_id": "T-CACHED", "scanned_at": "2026-09-30T10:00:00+05:30",
         "processed": processed, "total": total, "result": res}
    d.update(extra)
    return d


class TestScanPost:
    def test_delegates_to_start_scan_with_the_same_arguments(self, monkeypatch):
        seen = {}

        def fake_start(**kw):
            seen.update(kw)
            return {"task_id": "X"}

        monkeypatch.setattr(gw, "start_scan", fake_start)
        bt = BackgroundTasks()
        out = _run(gw.run_scan_post(force_refresh=False, lite=True, background_tasks=bt))
        assert out == {"task_id": "X"}
        assert seen == {"force_refresh": False, "lite": True, "background_tasks": bt}

    def test_missing_background_tasks_is_passed_through_not_swapped_for_a_dead_throwaway(self, monkeypatch):
        # FIXED: a direct call without BackgroundTasks used to hand start_scan a throwaway one that
        # nothing ever executed, so the scan was "started" but never ran. It now passes None through and
        # start_scan() runs the scan on a daemon thread (see TestStartScan).
        seen = {}

        def fake_start(**kw):
            seen.update(kw)
            return {}

        monkeypatch.setattr(gw, "start_scan", fake_start)
        _run(gw.run_scan_post())
        assert seen["background_tasks"] is None

    def test_post_scan_auto_selects_lite(self, tc, monkeypatch, kv):
        # FIXED: POST /scan no longer forces lite=False, so the open-circuit / SCAN_LITE_DEFAULT
        # protection applies on the overnight-cron path like GET /scan and POST /scan/start.
        monkeypatch.setattr(gw, "_should_force_lite_scan", lambda: True)
        monkeypatch.setattr(gw, "SCAN_LITE_DEFAULT", True)
        monkeypatch.setattr(gw, "_build_scan_universe", lambda: ["AAA", "BBB"])
        ran = []

        async def fake_parallel(task_id, universe, lite=False):
            ran.append(lite)

        monkeypatch.setattr(gw, "run_scan_parallel", fake_parallel)
        r = tc.post("/scan")
        assert r.status_code == 200 and r.json()["lite"] is True and r.json()["universe_size"] == 2
        assert ran == [True]
        r2 = tc.post("/scan/start?force_refresh=true")
        assert r2.json()["lite"] is True          # the sibling route does auto-select lite


class TestScanGet:
    @pytest.fixture(autouse=True)
    def _base(self, monkeypatch, kv):
        self.kv = kv
        self.universe = ["AAA", "BBB", "CCC"]
        self.ran = []
        self.parallel_exc = None
        self.on_run = None
        monkeypatch.setattr(gw, "_build_scan_universe", lambda: self.universe)
        monkeypatch.setattr(gw, "SCAN_LITE_DEFAULT", False)
        monkeypatch.setattr(gw, "_should_force_lite_scan", lambda: False)

        async def fake_parallel(task_id, universe, lite=False):
            self.ran.append((task_id, list(universe), lite))
            if self.on_run:
                self.on_run(task_id)
            if self.parallel_exc:
                raise self.parallel_exc

        monkeypatch.setattr(gw, "run_scan_parallel", fake_parallel)

    def _store_task(self, task_id, **data):
        self.kv.store[gw.SCAN_TASK_PREFIX + task_id] = data

    def test_force_refresh_clears_both_cache_keys_when_redis_is_on(self, rds):
        self.on_run = lambda tid: self._store_task(tid, result={"scanned": 1})
        _run(gw.run_scan(force_refresh=True))
        assert rds.deleted == [gw.SCAN_UNIVERSE_KEY, gw.LAST_FULL_SCAN_KEY]

    def test_force_refresh_redis_error_is_swallowed(self, rds):
        rds.raise_on_delete = True
        self.on_run = lambda tid: self._store_task(tid, result={"scanned": 1})
        assert _run(gw.run_scan(force_refresh=True)) == {"scanned": 1}

    def test_force_refresh_ignores_a_complete_cached_scan(self, rds):
        self.kv.store[gw.LAST_FULL_SCAN_KEY] = _cached_scan()
        self.on_run = lambda tid: self._store_task(tid, result={"fresh": True})
        assert _run(gw.run_scan(force_refresh=True)) == {"fresh": True}

    def test_complete_cached_scan_is_served_without_running(self):
        self.kv.store[gw.LAST_FULL_SCAN_KEY] = _cached_scan()
        out = _run(gw.run_scan())
        assert out["recommendations"] == [{"symbol": "AAA"}] and self.ran == []

    def test_cached_scan_at_exactly_90_percent_is_complete(self):
        self.kv.store[gw.LAST_FULL_SCAN_KEY] = _cached_scan(processed=270, total=300)
        assert _run(gw.run_scan())["recommendations"] and self.ran == []

    @pytest.mark.parametrize("cached", [
        _cached_scan(processed=269, total=300),
        _cached_scan(partial=True),
        _cached_scan(cancelled=True),
        _cached_scan(result={"scanned": 300, "universe_size": 300, "partial": True}),
        _cached_scan(processed=0, total=0, result={"x": 1}),
    ])
    def test_partial_or_empty_cached_scans_trigger_a_new_run(self, cached):
        self.kv.store[gw.LAST_FULL_SCAN_KEY] = cached
        self.on_run = lambda tid: self._store_task(tid, result={"fresh": True})
        assert _run(gw.run_scan()) == {"fresh": True} and len(self.ran) == 1

    def test_cache_entry_without_a_result_is_ignored(self):
        self.kv.store[gw.LAST_FULL_SCAN_KEY] = {"processed": 300, "total": 300, "result": None}
        self.on_run = lambda tid: self._store_task(tid, result={"fresh": True})
        assert _run(gw.run_scan()) == {"fresh": True}

    def test_processed_and_total_fall_back_to_the_result_block(self):
        cached = {"result": {"scanned": 100, "universe_size": 100, "recommendations": ["r"]}}
        self.kv.store[gw.LAST_FULL_SCAN_KEY] = cached
        assert _run(gw.run_scan())["recommendations"] == ["r"] and self.ran == []

    def test_empty_universe_returns_no_data(self):
        self.universe = []
        out = _run(gw.run_scan())
        assert out["verdict"] == "NO_DATA" and out["errors"] == ["empty_universe"]
        assert out["scanned"] == 0 and out["universe_size"] == 0 and self.ran == []
        assert out["scanned_at"].endswith("+05:30")

    @pytest.mark.parametrize("lite,default,forced,expect", [
        (True, False, False, True), (False, False, False, False),
        (False, True, False, True), (False, False, True, True), (True, False, True, True),
    ])
    def test_lite_selection(self, monkeypatch, lite, default, forced, expect):
        monkeypatch.setattr(gw, "SCAN_LITE_DEFAULT", default)
        monkeypatch.setattr(gw, "_should_force_lite_scan", lambda: forced)
        self.on_run = lambda tid: self._store_task(tid, result={"ok": 1})
        _run(gw.run_scan(lite=lite))
        assert self.ran[0][2] is expect and self.ran[0][1] == self.universe

    def test_returns_the_stored_result_dict(self):
        self.on_run = lambda tid: self._store_task(tid, result={"verdict": "OK", "scanned": 3})
        assert _run(gw.run_scan()) == {"verdict": "OK", "scanned": 3}

    def test_task_without_a_nested_result_returns_the_fallback_shape(self):
        self.on_run = lambda tid: self._store_task(tid, status="running", processed=2, total=3,
                                                   error="upstream slow")
        out = _run(gw.run_scan())
        assert out["scanned"] == 2 and out["universe_size"] == 3
        assert out["errors"] == ["upstream slow"] and out["status"] == "running"
        assert out["verdict"] == "UNKNOWN" and out["parallel"] is True
        assert out["recommendations"] == [] and out["all_results"] == []
        assert out["task_id"] == self.ran[0][0] and "deprecated_note" in out

    def test_fallback_shape_when_nothing_was_stored(self):
        out = _run(gw.run_scan())
        assert out["scanned"] == 0 and out["universe_size"] == len(self.universe)
        assert out["errors"] == [] and out["status"] is None

    def test_an_empty_result_dict_is_treated_as_missing(self):
        self.on_run = lambda tid: self._store_task(tid, result={}, processed=3, total=3)
        out = _run(gw.run_scan())
        assert out["parallel"] is True and out["scanned"] == 3 and out["verdict"] == "UNKNOWN"

    def test_non_dict_result_uses_unknown_defaults(self):
        self.on_run = lambda tid: self._store_task(tid, result="weird")
        out = _run(gw.run_scan())
        assert out["recommendations"] == [] and out["all_results"] == [] and out["verdict"] == "UNKNOWN"

    def test_parallel_failure_returns_a_result_that_was_stored_before_the_crash(self, logs):
        self.parallel_exc = RuntimeError("worker died")
        self.on_run = lambda tid: self._store_task(tid, result={"partial": True, "scanned": 1})
        assert _run(gw.run_scan()) == {"partial": True, "scanned": 1}
        assert any("legacy /scan parallel wrapper failed" in m for m in logs["error"])

    def test_parallel_failure_without_a_result_is_a_500(self):
        self.parallel_exc = RuntimeError("x" * 500)
        with pytest.raises(HTTPException) as e:
            _run(gw.run_scan())
        assert e.value.status_code == 500 and e.value.detail == "Scan failed: " + "x" * 200

    def test_served_over_http(self, tc):
        self.on_run = lambda tid: self._store_task(tid, result={"verdict": "OK"})
        r = tc.get("/scan?lite=true")
        assert r.status_code == 200 and r.json() == {"verdict": "OK"} and self.ran[0][2] is True


# ── POST /scan/batch ─────────────────────────────────────────────────────────

class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class TestScanBatch:
    @pytest.fixture(autouse=True)
    def _base(self, monkeypatch):
        self.shared = object()
        self.analysed = []
        self.feeds = {}
        self.feed_exc = None
        self.feed_none = False
        self.per_symbol = {}          # sym -> callable | value | Exception
        monkeypatch.setattr(gw, "_get_http_client", lambda: self.shared)

        store = SimpleNamespace()

        def bulk(symbols):
            store.asked = list(symbols)
            if self.feed_exc:
                raise self.feed_exc
            return None if self.feed_none else self.feeds

        store.get_symbols_bulk = bulk
        self.store = store
        monkeypatch.setattr(gw, "_feed_store", lambda: store)

        async def fake_one(sym, client, sem, **kw):
            self.analysed.append((sym, client, sem, kw))
            out = self.per_symbol.get(sym, {"decision": "BUY"})
            if isinstance(out, Exception):
                raise out
            return out

        monkeypatch.setattr(gw, "_analyze_one_symbol_ultra", fake_one)

    def batch(self, symbols):
        return _run(gw.scan_batch(_Req({"symbols": symbols})))

    def test_normalises_symbols_and_enforces_them_on_results(self):
        out = self.batch(["reliance.ns", " tcs.bo ", "", "  ", "infy"])
        assert [r["symbol"] for r in out["results"]] == ["RELIANCE", "TCS", "INFY"]
        assert self.store.asked == ["RELIANCE", "TCS", "INFY"]

    def test_each_analysis_gets_the_shared_client_feed_row_and_skips_gemini(self):
        self.feeds = {"TCS": {"price": 1}}
        self.batch(["TCS", "INFY"])
        by_sym = {a[0]: a for a in self.analysed}
        assert by_sym["TCS"][1] is self.shared
        assert by_sym["TCS"][3]["feed_row"] == {"price": 1} and by_sym["INFY"][3]["feed_row"] is None
        assert by_sym["TCS"][3]["prefetched_feeds"] is self.feeds
        assert all(a[3]["skip_gemini"] is True for a in self.analysed)
        assert by_sym["TCS"][2]._value == 8        # semaphore sized to MAX_PARALLEL_WORKERS

    def test_a_15_symbol_batch_is_allowed_and_16_is_rejected(self):
        assert len(self.batch([f"S{i}" for i in range(15)])["results"]) == 15
        with pytest.raises(HTTPException) as e:
            self.batch([f"S{i}" for i in range(16)])
        assert e.value.status_code == 400 and "Maximum 15" in e.value.detail

    def test_blank_entries_do_not_count_toward_the_cap(self):
        # FIXED: the cap was checked on the raw list, so 15 real symbols plus a blank was refused.
        out = self.batch([f"S{i}" for i in range(15)] + ["", "  "])
        assert len(out["results"]) == 15
        with pytest.raises(HTTPException):
            self.batch([f"S{i}" for i in range(16)] + [""])

    def test_bulk_prefetch_failure_is_not_fatal(self):
        self.feed_exc = RuntimeError("neon down")
        out = self.batch(["TCS"])
        assert out["results"][0]["decision"] == "BUY" and self.analysed[0][3]["feed_row"] is None
        assert self.analysed[0][3]["prefetched_feeds"] == {}

    def test_bulk_prefetch_returning_none_is_an_empty_map(self):
        self.feed_none = True
        self.batch(["TCS"])
        assert self.analysed[0][3]["prefetched_feeds"] == {}

    def test_one_failing_symbol_becomes_an_error_row(self):
        self.per_symbol["INFY"] = RuntimeError("scorer blew up")
        out = self.batch(["TCS", "INFY"])
        assert out["results"][0]["decision"] == "BUY"
        assert out["results"][1] == {"symbol": "INFY", "decision": "ERROR", "error": "scorer blew up"}

    def test_a_non_dict_result_is_passed_through_unchanged(self):
        self.per_symbol["TCS"] = "weird"
        assert self.batch(["TCS"])["results"] == ["weird"]

    def test_no_symbols_key_returns_empty_results(self):
        assert _run(gw.scan_batch(_Req({})))["results"] == []

    def test_a_bare_string_is_one_symbol_not_characters(self):
        # FIXED: {"symbols": "TCS"} was iterated char-by-char -> T, C, S.
        out = _run(gw.scan_batch(_Req({"symbols": "TCS"})))
        assert [r["symbol"] for r in out["results"]] == ["TCS"]

    def test_a_comma_separated_string_is_split_into_symbols(self):
        out = _run(gw.scan_batch(_Req({"symbols": "tcs.ns, infy ;reliance,"})))
        assert [r["symbol"] for r in out["results"]] == ["TCS", "INFY", "RELIANCE"]

    def test_malformed_bodies_are_400s(self, tc):
        # FIXED: no body validation — null body / null symbols / bad JSON were bare 500s.
        hdr = {"content-type": "application/json"}
        r = tc.post("/scan/batch", content="null", headers=hdr)
        assert r.status_code == 400 and "JSON object" in r.json()["detail"]
        r = tc.post("/scan/batch", content="[1]", headers=hdr)
        assert r.status_code == 400
        r = tc.post("/scan/batch", content="{not json", headers=hdr)
        assert r.status_code == 400 and "must be JSON" in r.json()["detail"]
        r = tc.post("/scan/batch", json={"symbols": None})
        assert r.status_code == 400 and "list" in r.json()["detail"]
        r = tc.post("/scan/batch", json={"symbols": {"a": 1}})
        assert r.status_code == 400

    def test_served_over_http(self, tc):
        r = tc.post("/scan/batch", json={"symbols": ["tcs.ns"]})
        assert r.status_code == 200 and r.json()["results"][0]["symbol"] == "TCS"


# ── POST /scan/start ─────────────────────────────────────────────────────────

class TestStartScan:
    @pytest.fixture(autouse=True)
    def _base(self, monkeypatch, kv):
        self.kv = kv
        self.universe = ["AAA", "BBB"]
        self.build_exc = None
        monkeypatch.setattr(gw, "SCAN_LITE_DEFAULT", False)
        monkeypatch.setattr(gw, "_should_force_lite_scan", lambda: False)

        def build():
            if self.build_exc:
                raise self.build_exc
            return self.universe

        monkeypatch.setattr(gw, "_build_scan_universe", build)

    def start(self, **kw):
        bt = BackgroundTasks()
        kw.setdefault("background_tasks", bt)
        return gw.start_scan(**kw), kw["background_tasks"]

    def test_cold_start_queues_the_parallel_run(self):
        out, bt = self.start()
        assert out["from_cache"] is False and out["universe_size"] == 2 and out["lite"] is False
        assert out["message"] == ("Scanning 2 symbols (dynamic universe; Neon/batch cache for hits, "
                                  "upstream for rest)")
        uuid.UUID(out["task_id"])
        task = bt.tasks[0]
        assert task.func is gw.run_scan_parallel and task.args == (out["task_id"], self.universe, False)

    @pytest.mark.parametrize("lite,default,forced,expect", [
        (None, False, False, False), (None, True, False, True), (None, False, True, True),
        (True, False, False, True), (False, True, True, False),
    ])
    def test_lite_selection_none_means_auto_but_false_is_respected(self, monkeypatch, lite, default,
                                                                   forced, expect):
        monkeypatch.setattr(gw, "SCAN_LITE_DEFAULT", default)
        monkeypatch.setattr(gw, "_should_force_lite_scan", lambda: forced)
        out, bt = self.start(lite=lite)
        assert out["lite"] is expect and bt.tasks[0].args[2] is expect

    def test_force_refresh_with_redis_deletes_universe_then_last_scan_then_universe_again(self, rds):
        self.start(force_refresh=True)
        assert rds.deleted == [gw.SCAN_UNIVERSE_KEY, gw.LAST_FULL_SCAN_KEY, gw.SCAN_UNIVERSE_KEY]

    def test_force_refresh_redis_errors_are_swallowed(self, rds):
        rds.raise_on_delete = True
        out, _ = self.start(force_refresh=True)
        assert out["from_cache"] is False

    def test_force_refresh_skips_the_complete_cache(self):
        self.kv.store[gw.LAST_FULL_SCAN_KEY] = _cached_scan()
        out, bt = self.start(force_refresh=True)
        assert out["from_cache"] is False and len(bt.tasks) == 1

    def test_recent_complete_scan_is_reused_as_a_done_task(self):
        res = {"scanned": 300, "universe_size": 300, "elapsed_seconds": 42.5}
        self.kv.store[gw.LAST_FULL_SCAN_KEY] = _cached_scan(result=res, processed=300, total=300)
        out, bt = self.start()
        assert out["from_cache"] is True and out["task_id"] == "T-CACHED"
        assert out["universe_size"] == 300 and out["scanned_at"] == "2026-09-30T10:00:00+05:30"
        assert "Returning recent complete scan" in out["message"] and bt.tasks == []
        key, val, ttl = self.kv.sets[-1]
        assert key == gw.SCAN_TASK_PREFIX + "T-CACHED" and ttl == 3600
        assert val["status"] == "done" and val["from_cache"] is True and val["elapsed"] == 42.5
        assert val["result"] is res and val["error"] is None

    def test_cached_scan_without_a_task_id_gets_a_fresh_one(self):
        c = _cached_scan()
        c.pop("task_id")
        self.kv.store[gw.LAST_FULL_SCAN_KEY] = c
        out, _ = self.start()
        uuid.UUID(out["task_id"])
        assert self.kv.sets[-1][0] == gw.SCAN_TASK_PREFIX + out["task_id"]

    def test_processed_and_total_fall_back_to_the_result_block(self):
        self.kv.store[gw.LAST_FULL_SCAN_KEY] = {"result": {"scanned": 50, "universe_size": 50}}
        out, _ = self.start()
        assert out["from_cache"] is True and out["universe_size"] == 50

    def test_total_falls_back_to_processed(self):
        self.kv.store[gw.LAST_FULL_SCAN_KEY] = {"processed": 40, "result": {"x": 1}}
        out, _ = self.start()
        assert out["from_cache"] is True and out["universe_size"] == 40

    @pytest.mark.parametrize("cached", [
        _cached_scan(processed=269, total=300),
        _cached_scan(partial=True), _cached_scan(cancelled=True),
        _cached_scan(result={"scanned": 300, "universe_size": 300, "partial": True}),
        _cached_scan(result={"scanned": 300, "universe_size": 300, "stopped_early": True}),
        _cached_scan(result={"scanned": 300, "universe_size": 300, "cancelled": True}),
        _cached_scan(processed=0, total=0, result={"x": 1}),
    ])
    def test_partial_scans_continue_instead_of_being_served(self, cached, logs):
        self.kv.store[gw.LAST_FULL_SCAN_KEY] = cached
        out, bt = self.start()
        assert out["from_cache"] is False and len(bt.tasks) == 1
        assert any("Last scan was partial" in m for m in logs["info"])

    def test_cache_entry_that_is_not_a_dict_is_ignored(self):
        self.kv.store[gw.LAST_FULL_SCAN_KEY] = "garbage"
        out, _ = self.start()
        assert out["from_cache"] is False

    def test_universe_build_failure_is_a_500(self, logs):
        self.build_exc = RuntimeError("nse down")
        with pytest.raises(HTTPException) as e:
            self.start()
        assert e.value.status_code == 500 and e.value.detail == "Scan failed: nse down"
        assert any("Scan start failed" in m for m in logs["error"])

    def test_calling_without_background_tasks_runs_the_scan_on_a_daemon_thread(self, monkeypatch):
        # FIXED: the `background_tasks=None` default used to fail with AttributeError (reported as a
        # 500). It now runs run_scan_parallel on a daemon thread with its own event loop.
        import threading
        ran, done = [], threading.Event()

        async def fake_parallel(task_id, universe, lite=False):
            ran.append((task_id, universe, lite, threading.current_thread().daemon))
            done.set()

        monkeypatch.setattr(gw, "run_scan_parallel", fake_parallel)
        out = gw.start_scan(lite=True)
        assert out["from_cache"] is False and out["lite"] is True and out["universe_size"] == 2
        assert done.wait(5)
        assert ran == [(out["task_id"], self.universe, True, True)]

    def test_a_crashing_fallback_scan_thread_is_logged_not_raised(self, monkeypatch, logs):
        import threading
        done = threading.Event()

        async def boom(task_id, universe, lite=False):
            done.set()
            raise RuntimeError("scan blew up")

        monkeypatch.setattr(gw, "run_scan_parallel", boom)
        out = gw.start_scan()
        assert out["from_cache"] is False
        assert done.wait(5)
        for _ in range(100):                      # the logger call lands just after the event
            if any("crashed: scan blew up" in m for m in logs["error"]):
                break
            threading.Event().wait(0.05)
        assert any("crashed: scan blew up" in m for m in logs["error"])

    def test_post_scan_without_background_tasks_really_runs_end_to_end(self, monkeypatch):
        import threading
        done, ran = threading.Event(), []

        async def fake_parallel(task_id, universe, lite=False):
            ran.append(task_id)
            done.set()

        monkeypatch.setattr(gw, "run_scan_parallel", fake_parallel)
        out = _run(gw.run_scan_post(force_refresh=False, lite=False))
        assert done.wait(5) and ran == [out["task_id"]]

    def test_served_over_http_and_the_queued_run_executes(self, tc, monkeypatch):
        ran = []

        async def fake_parallel(task_id, universe, lite=False):
            ran.append((task_id, universe, lite))

        monkeypatch.setattr(gw, "run_scan_parallel", fake_parallel)
        r = tc.post("/scan/start?lite=true")
        assert r.status_code == 200 and r.json()["lite"] is True
        assert ran == [(r.json()["task_id"], ["AAA", "BBB"], True)]


# ── GET /scan/last, GET /scan/status/{task_id} ───────────────────────────────

class TestScanLast:
    def test_nothing_cached(self, kv):
        assert gw.get_last_scan() == {"ok": False, "detail": "No scan cached yet", "result": None}

    def test_non_dict_cache_entry(self, kv):
        kv.store[gw.LAST_FULL_SCAN_KEY] = ["x"]
        assert gw.get_last_scan()["ok"] is False

    def test_cached_scan_shape(self, kv):
        kv.store[gw.LAST_FULL_SCAN_KEY] = _cached_scan(processed=120, total=300)
        out = gw.get_last_scan()
        assert out == {"ok": True, "task_id": "T-CACHED", "scanned_at": "2026-09-30T10:00:00+05:30",
                       "partial": True, "processed": 120, "total": 300,
                       "result": kv.store[gw.LAST_FULL_SCAN_KEY]["result"]}

    def test_partial_flag_follows_the_same_rule_as_scan_and_scan_start(self, kv):
        kv.store[gw.LAST_FULL_SCAN_KEY] = _cached_scan(partial=True)
        assert gw.get_last_scan()["partial"] is True
        kv.store[gw.LAST_FULL_SCAN_KEY] = _cached_scan(cancelled=True)
        assert gw.get_last_scan()["partial"] is True
        # FIXED: the result block's own flags and the 90% rule are now consulted too.
        for flag in ("partial", "stopped_early", "cancelled"):
            res = {"scanned": 300, "universe_size": 300, flag: True}
            kv.store[gw.LAST_FULL_SCAN_KEY] = _cached_scan(result=res)
            assert gw.get_last_scan()["partial"] is True, flag
        kv.store[gw.LAST_FULL_SCAN_KEY] = _cached_scan(processed=10, total=300)
        assert gw.get_last_scan()["partial"] is True
        kv.store[gw.LAST_FULL_SCAN_KEY] = _cached_scan(processed=269, total=300)
        assert gw.get_last_scan()["partial"] is True
        kv.store[gw.LAST_FULL_SCAN_KEY] = _cached_scan(processed=270, total=300)
        assert gw.get_last_scan()["partial"] is False
        kv.store[gw.LAST_FULL_SCAN_KEY] = _cached_scan(processed=300, total=300)
        assert gw.get_last_scan()["partial"] is False

    def test_unparseable_counts_do_not_break_the_partial_check(self, kv):
        kv.store[gw.LAST_FULL_SCAN_KEY] = _cached_scan(processed="n/a", total="n/a")
        out = gw.get_last_scan()
        assert out["ok"] is True and out["partial"] is False

    def test_non_dict_result_block_is_tolerated(self, kv):
        kv.store[gw.LAST_FULL_SCAN_KEY] = _cached_scan(result="junk", processed=300, total=300)
        assert gw.get_last_scan()["partial"] is False

    def test_total_falls_back_to_universe_size(self, kv):
        c = _cached_scan()
        c.pop("total")
        c["universe_size"] = 77
        kv.store[gw.LAST_FULL_SCAN_KEY] = c
        assert gw.get_last_scan()["total"] == 77

    def test_served_over_http(self, tc, kv):
        assert tc.get("/scan/last").json()["ok"] is False


class TestScanStatus:
    def test_unknown_task_is_a_soft_response_not_a_404(self, kv, tc):
        out = gw.get_scan_status("nope")
        assert out == {"task_id": "nope", "status": "unknown", "processed": 0, "total": 0,
                       "cancelled": True, "partial": True, "ok": False,
                       "message": "Scan task not found (expired or power-off). Safe to start a new scan."}
        assert tc.get("/scan/status/nope").status_code == 200

    def test_running_task_gets_an_eta(self, kv):
        kv.store[gw.SCAN_TASK_PREFIX + "t1"] = {"status": "running", "processed": 50, "total": 200,
                                                "elapsed": 100}
        assert gw.get_scan_status("t1")["estimated_remaining"] == 300.0

    def test_eta_is_rounded_to_one_decimal(self, kv):
        kv.store[gw.SCAN_TASK_PREFIX + "t1"] = {"status": "running", "processed": 3, "total": 10,
                                                "elapsed": 10}
        assert gw.get_scan_status("t1")["estimated_remaining"] == 23.3

    @pytest.mark.parametrize("data", [
        {"status": "running", "processed": 0, "total": 10, "elapsed": 5},
        {"status": "running", "processed": 4, "total": 10, "elapsed": 0},
        {"status": "running"},
    ])
    def test_eta_is_none_until_there_is_progress(self, kv, data):
        kv.store[gw.SCAN_TASK_PREFIX + "t1"] = dict(data)
        assert gw.get_scan_status("t1")["estimated_remaining"] is None

    def test_finished_task_is_returned_as_stored(self, kv):
        kv.store[gw.SCAN_TASK_PREFIX + "t1"] = {"status": "done", "processed": 9, "total": 9}
        assert gw.get_scan_status("t1") == {"status": "done", "processed": 9, "total": 9}

    def test_eta_is_clamped_to_zero_when_processed_exceeds_total(self, kv):
        # FIXED: no clamp on remaining = total - processed, so the ETA went negative (-2.0).
        kv.store[gw.SCAN_TASK_PREFIX + "t1"] = {"status": "running", "processed": 12, "total": 10,
                                                "elapsed": 12}
        assert gw.get_scan_status("t1")["estimated_remaining"] == 0.0


# ── _lite_evaluate_from_feed ─────────────────────────────────────────────────

class TestLiteEvaluate:
    @pytest.fixture
    def scorer(self, monkeypatch):
        import instant_scanner
        st = SimpleNamespace(calls=[], exc=None)

        def fake(symbol, feed, tick):
            st.calls.append((symbol, feed, tick))
            if st.exc:
                raise st.exc
            return {"from": "instant_scanner", "symbol": symbol}

        monkeypatch.setattr(instant_scanner, "compute_instant_scores", fake)
        return st

    def test_delegates_to_instant_scanner_with_a_price_tick(self, scorer):
        out = gw._lite_evaluate_from_feed("TCS", {"rsi": 50}, 101.5)
        assert out == {"from": "instant_scanner", "symbol": "TCS"}
        assert scorer.calls == [("TCS", {"rsi": 50}, {"price": 101.5})]

    @pytest.mark.parametrize("px", [0.0, -5.0, None])
    def test_no_positive_price_means_an_empty_tick(self, scorer, px):
        gw._lite_evaluate_from_feed("TCS", {}, px)
        assert scorer.calls[0][2] == {}

    def test_non_dict_feed_is_passed_as_empty(self, scorer):
        gw._lite_evaluate_from_feed("TCS", "garbage", 10.0)
        assert scorer.calls[0][1] == {}

    def test_scorer_failure_falls_back_to_a_minimal_hold_card(self, scorer, logs):
        scorer.exc = RuntimeError("scorer bug")
        out = gw._lite_evaluate_from_feed("tcs.ns", {"fundamental_score": 64}, 250.0)
        assert out["symbol"] == "TCS" and out["decision"] == "HOLD" and out["confidence"] == "Low"
        assert out["combined_score"] == 50 and out["technical_score"] == 50
        assert out["fundamental_score"] == 64 and out["lite_fastpath"] is True
        assert out["status"] == "READY"
        assert out["close"] == out["price"] == 250.0 and out["ltp"] == 250.0
        assert any("instant_scanner failed for tcs.ns" in m for m in logs["warning"])

    def test_fallback_defaults_fundamental_score_to_50_and_uses_the_feed_price(self, scorer):
        scorer.exc = RuntimeError("x")
        out = gw._lite_evaluate_from_feed("TCS", {"price": 99.0}, 0.0)
        assert out["fundamental_score"] == 50 and out["price"] == 99.0

    def test_fallback_with_no_price_anywhere_skips_the_aliases(self, scorer):
        scorer.exc = RuntimeError("x")
        out = gw._lite_evaluate_from_feed("TCS", {}, 0.0)
        assert out["price"] == 0.0 and out["close"] == 0.0 and "ltp" not in out

    def test_fallback_with_a_non_dict_feed(self, scorer):
        scorer.exc = RuntimeError("x")
        out = gw._lite_evaluate_from_feed("TCS", None, 12.0)
        assert out["fundamental_score"] == 50 and out["price"] == 12.0

    def test_second_level_fallback_when_price_resolution_also_fails(self, scorer, monkeypatch):
        import price_resolver
        scorer.exc = RuntimeError("x")

        def boom(*a, **k):
            raise RuntimeError("resolver bug")

        monkeypatch.setattr(price_resolver, "extract_safe_price", boom)
        out = gw._lite_evaluate_from_feed("tcs.bo", {"fundamental_score": 70}, 33.0)
        assert out == {"symbol": "TCS", "decision": "HOLD", "combined_score": 50,
                       "technical_score": 50, "fundamental_score": 50, "price": 33.0,
                       "lite_fastpath": True}


# ── group 81: decide timeout, one retry, overall deadline ───────────────────────────────────

class _ClockShim:
    """Stand-in for the `time` module as seen by main.py ONLY (patching time.monotonic globally would
    also freeze asyncio's event loop). Delegates everything else to the real module."""

    def __init__(self, fn):
        import time as _real
        self._real = _real
        self.monotonic = fn

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestStockTimeBudget:
    def _decide_calls(self, env):
        return [(u, kw) for _, u, kw in env.client.calls if u.startswith(D("TCS"))]

    def test_defaults(self):
        assert gw.STOCK_DECIDE_TIMEOUT_SEC == 60.0
        assert gw.STOCK_OVERALL_DEADLINE_SEC == 100.0      # under the browser's 120 s request timeout
        assert gw.STOCK_DECIDE_RETRY_MIN_SEC == 15.0

    @pytest.mark.parametrize("raw,want", [
        (None, 7.0), ("", 7.0), ("  ", 7.0), ("abc", 7.0), ("nan", 7.0), ("inf", 7.0),
        ("-5", 7.0), ("0", 7.0), ("1", 7.0),                 # below the allowed floor (5)
        ("500", 7.0),                                          # above the allowed ceiling (170)
        ("45", 45.0), (" 45.5 ", 45.5), ("5", 5.0), ("170", 170.0),
    ])
    def test_budget_env_parsing_is_blank_and_garbage_safe(self, monkeypatch, raw, want):
        if raw is None:
            monkeypatch.delenv("X_BUDGET", raising=False)
        else:
            monkeypatch.setenv("X_BUDGET", raw)
        assert gw._stock_budget_sec("X_BUDGET", 7.0, 5.0, 170.0) == want

    def test_a_slow_first_decide_is_retried_once_without_force(self, env):
        seen = []

        def _decide(url, kw):
            seen.append(kw["params"]["force"])
            if len(seen) == 1:
                return httpx.ReadTimeout("cold caches")
            return FakeResp(200, _full_decide())

        env.client.routes[D("TCS")] = _decide
        out = stock("TCS")
        assert out["decision"] == "BUY" and "error" not in out
        assert seen == ["true", "false"]

    def test_decide_call_timeout_is_bounded_by_the_remaining_budget(self, env, monkeypatch):
        monkeypatch.setattr(gw, "STOCK_DECIDE_TIMEOUT_SEC", 60.0)
        monkeypatch.setattr(gw, "STOCK_OVERALL_DEADLINE_SEC", 30.0)
        env.decide("TCS")
        stock("TCS")
        (url, kw), = self._decide_calls(env)
        assert kw["timeout"].read <= 30.0 and kw["timeout"].connect == 5.0

    def test_no_retry_when_too_little_budget_is_left(self, env, monkeypatch):
        monkeypatch.setattr(gw, "STOCK_DECIDE_RETRY_MIN_SEC", 500.0)     # never enough
        env.client.routes[D("TCS")] = httpx.ReadTimeout("slow")
        out = stock("TCS")
        assert len(self._decide_calls(env)) == 1
        assert out["decision"] == "HOLD"

    def test_only_a_read_timeout_is_retried(self, env):
        env.client.routes[D("TCS")] = httpx.ConnectError("refused")
        stock("TCS")
        assert len(self._decide_calls(env)) == 1

    def test_still_slow_after_the_retry_gives_an_honest_timeout_hold(self, env, logs):
        env.client.routes[D("TCS")] = httpx.ReadTimeout("slow")
        out = stock("TCS")
        assert len(self._decide_calls(env)) == 2
        assert out["decision"] == "HOLD" and out["data_insufficient"] is True
        assert out["error"].startswith("decision engine timed out after ") and "ReadTimeout" in out["error"]
        assert "taking longer than usual" in out["natural_language_summary"]
        assert "unreachable" not in out["natural_language_summary"]
        assert out["data_quality"]["flags"] == ["Decision engine slow"]
        assert any("timed out for TCS" in m for m in logs["warning"])
        assert any("retrying once without force" in m for m in logs["warning"])

    def test_enrichment_calls_never_outlive_the_overall_deadline(self, env, monkeypatch):
        monkeypatch.setattr(gw, "STOCK_OVERALL_DEADLINE_SEC", 20.0)
        env.passthrough()
        env.decide("TCS", {"decision": "BUY", "combined_score": 60, "close": 100.0,
                           "technical_score": None, "fundamental_score": None, "news_score": None,
                           "prediction_score": None, "reasons": {}})
        for u in (T("TCS"), F("TCS"), N("TCS"), E("TCS"), P("TCS")):
            env.client.routes[u] = FakeResp(200, {})
        stock("TCS")
        timeouts = [kw["timeout"] for _, u, kw in env.client.calls if not u.startswith(D("TCS"))]
        assert timeouts and all(t <= 20.0 for t in timeouts)        # 60 / 45 / 30 clamped to the budget

    def test_slow_enrichment_is_dropped_at_the_deadline_and_finished_ones_are_kept(self, env, monkeypatch, logs):
        import time as real_time
        monkeypatch.setattr(gw, "STOCK_OVERALL_DEADLINE_SEC", 10.0)
        env.passthrough()
        # decide lacks news and events -> both are enriched concurrently
        env.decide("TCS", {"decision": "BUY", "combined_score": 60, "close": 100.0,
                           "technical_score": 61, "fundamental_score": 62, "news_score": None,
                           "prediction_score": 63, "fundamental_metrics": {"pe": 1},
                           "reasons": {"technical": ["ok"], "fundamental": ["ok"]}})
        env.client.routes[E("TCS")] = FakeResp(200, {"next_earnings_date": "2026-11-01"})
        base = real_time.monotonic()
        clock = {"t": 0.0}
        monkeypatch.setattr(gw, "time", _ClockShim(lambda: base + clock["t"]))
        scripted_get = env.client.get

        async def _get(url, **kw):
            if url.startswith(N("TCS")):
                clock["t"] = 11.0           # the deadline passes while the news call is still pending
                await asyncio.sleep(30)
            return await scripted_get(url, **kw)

        env.client.get = _get
        out = asyncio.run(asyncio.wait_for(gw.get_stock_decision("TCS"), timeout=20))
        assert out["news_score"] is None
        assert "news" not in out["enrichment"]["fetched"]
        assert "events" in out["enrichment"]["fetched"]               # the finished call is kept
        assert any("deadline hit" in m and "news" in m for m in logs["warning"])

    def test_gemini_is_skipped_when_the_budget_is_spent(self, env, monkeypatch):
        import time as real_time
        monkeypatch.setattr(gw, "STOCK_OVERALL_DEADLINE_SEC", 10.0)
        env.decide("TCS")
        calls = []

        async def _ai(result, client):
            calls.append(1)
            return "AI-SUMMARY"

        monkeypatch.setattr(gw, "_generate_ai_summary", _ai)
        base = real_time.monotonic()
        clock = {"t": 0.0}
        monkeypatch.setattr(gw, "time", _ClockShim(lambda: base + clock["t"]))
        scripted_get = env.client.get

        async def _get(url, **kw):
            clock["t"] = 9.0                # decide returns 9 s into a 10 s budget -> 1 s left (< 3 s reserve)
            return await scripted_get(url, **kw)

        env.client.get = _get
        out = stock("TCS")
        assert calls == [] and out["natural_language_summary"] == "TEMPLATE-SUMMARY"

    def test_a_hung_gemini_call_is_cut_off_by_the_budget(self, env, monkeypatch):
        monkeypatch.setattr(gw, "STOCK_OVERALL_DEADLINE_SEC", 3.5)
        env.decide("TCS")

        async def _ai(result, client):
            await asyncio.sleep(60)
            return "AI-SUMMARY"

        monkeypatch.setattr(gw, "_generate_ai_summary", _ai)
        out = stock("TCS")
        assert out["natural_language_summary"] == "TEMPLATE-SUMMARY"
