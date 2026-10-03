"""tests/test_main_ops_routes.py — coverage for api-gateway/main.py, slice 5 (lines 3856-4595)

Pass 63. The small "plumbing" routes between the scan runner and `/stock/{symbol}`:

* `/` (service card + endpoint index), `/quote/{symbol}` (market-data proxy with a yfinance
  fallback), `/circuits`, `/ops/rate-limits[/event]`, `/metrics`;
* `/ops/check-alert` (cron alerting thresholds + notify), `/ops/refresh-static-params`
  (nightly cache warm-up), `/market/history/{symbol}` (market-data then training fallback);
* `/ops/db-status`, `_neon_keepalive_ping`, `/ops/neon-keepalive`, `/ops/wake-db-all`,
  `/ops/idle-tick`, `/ops/qstash/tick|publish`, `/wake-all`, `/wake/all`;
* `/market/momentum-movers`, `/health`, `/ready`, `/system/health`;
* watchlist CRUD, `/events/{symbol}`, `/searched`.

Handlers that take a `Request` (or need route wiring) are driven through Starlette's TestClient
WITHOUT the context manager, so the app's start-up hooks never run. Everything downstream is
faked: the shared async httpx client (one scripted `FakeClient`), the sync `httpx.get`, Redis,
the kv cache, metrics, circuit snapshots, the rate-limit monitor, qstash, and `asyncio.sleep`.
Nothing touches the network. Findings are pinned as current behaviour and marked ``NOT FIXED``;
ones that have since been fixed say ``Fixed`` in their comment.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_ops_routes.py -v
"""
from __future__ import annotations

import asyncio
import os
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

from fastapi import HTTPException
from fastapi.testclient import TestClient

_REAL_SLEEP = asyncio.sleep


def _run(coro):
    return asyncio.run(coro)


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status=200, data=None, content=True, json_raises=False):
        self.status_code = status
        self._data = {} if data is None else data
        self.content = b"x" if content else b""
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
        self.default = None

    def _resolve(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        for frag, out in self.routes.items():
            if frag in url:
                if callable(out):
                    out = out(url, kwargs)
                if isinstance(out, Exception):
                    raise out
                return out
        if self.default is not None:
            if isinstance(self.default, Exception):
                raise self.default
            return self.default
        raise httpx.ConnectError("no route for " + url)

    async def get(self, url, **kw):
        return self._resolve("GET", url, kw)

    async def post(self, url, **kw):
        return self._resolve("POST", url, kw)

    def urls(self, method="GET"):
        return [u for m, u, _ in self.calls if m == method]


class FakeMetrics:
    def __init__(self):
        self.incs, self.gauges = [], []
        self.raise_on_inc = False
        self.snap = {"counters": {}}

    def inc(self, name, *a, **k):
        if self.raise_on_inc:
            raise RuntimeError("metrics down")
        self.incs.append((name, k))

    def set_gauge(self, name, value, *a, **k):
        self.gauges.append((name, value))

    def snapshot(self):
        return self.snap

    def prometheus_text(self):
        return "# prom text\nx 1\n"


class FakeRedis:
    def __init__(self):
        self.deleted = []
        self.raise_on = set()

    def delete(self, key):
        if key in self.raise_on or "*" in self.raise_on:
            raise RuntimeError("redis down")
        self.deleted.append(key)


class FakeMonitor:
    def __init__(self):
        self.recorded, self.snap_args, self.raise_on_record = [], [], False

    def record(self, **kw):
        if self.raise_on_record:
            raise ValueError("bad record")
        self.recorded.append(kw)

    def snapshot(self, circuits=None):
        self.snap_args.append(circuits)
        return {"events": [], "circuits": circuits}


@pytest.fixture
def client(monkeypatch):
    c = FakeClient()
    monkeypatch.setattr(gw, "_get_http_client", lambda: c)
    return c


@pytest.fixture
def fm(monkeypatch):
    m = FakeMetrics()
    monkeypatch.setattr(gw, "metrics", m)
    return m


@pytest.fixture
def tc():
    return TestClient(gw.app, raise_server_exceptions=False)


@pytest.fixture
def logs(monkeypatch):
    rec = {"warning": [], "debug": []}

    class L:
        def warning(self, msg, *a, **k):
            rec["warning"].append(msg % a if a else msg)

        def debug(self, msg, *a, **k):
            rec["debug"].append(msg % a if a else msg)

        def info(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

    monkeypatch.setattr(gw, "logger", L())
    return rec


@pytest.fixture
def fast_sleep(monkeypatch):
    sleeps = []

    async def _s(d, *a, **k):
        sleeps.append(d)

    monkeypatch.setattr(gw.asyncio, "sleep", _s)
    return sleeps


# ── root ─────────────────────────────────────────────────────────────────────

class TestRoot:
    def test_service_card(self):
        out = gw.root()
        assert out["service"] == "Stockky API Gateway"
        assert out["status"] == "running"
        assert out["version"] == gw.app.version == "2.5.16"
        assert out["parallel_workers"] == gw.MAX_PARALLEL_WORKERS

    def test_every_documented_endpoint_is_a_registered_route(self):
        registered = {getattr(r, "path", None) for r in gw.app.routes}
        for path in gw.root()["endpoints"]:
            assert path in registered, path

    def test_no_route_is_registered_twice_for_the_same_method(self):
        # session72 regression guard: a 2nd `wake_all_services` once shadowed /wake-all.
        seen = {}
        for r in gw.app.routes:
            for m in getattr(r, "methods", None) or ():
                key = (getattr(r, "path", None), m)
                seen[key] = seen.get(key, 0) + 1
        assert [k for k, v in seen.items() if v > 1] == []

    def test_index_documents_the_ops_routes_it_sits_beside(self):
        # FIXED: /quote, /circuits, /metrics and /ops/check-alert are listed.
        doc = gw.root()["endpoints"]
        for p in ("/quote/{symbol}", "/circuits", "/metrics", "/ops/check-alert"):
            assert p in doc

    def test_served_over_http(self, tc):
        r = tc.get("/")
        assert r.status_code == 200 and r.json()["service"] == "Stockky API Gateway"


# ── /quote/{symbol} ──────────────────────────────────────────────────────────

class TestQuote:
    @pytest.fixture
    def md(self, monkeypatch):
        state = SimpleNamespace(resp=FakeResp(200, {"price": 101.5}), exc=None, urls=[], kw=[])

        def _get(url, **kw):
            state.urls.append(url)
            state.kw.append(kw)
            if state.exc:
                raise state.exc
            return state.resp

        monkeypatch.setattr(gw.httpx, "get", _get)
        return state

    def test_market_data_success_shape(self, md, fm):
        md.resp = FakeResp(200, {"price": 101.5, "close": 100.0, "as_of": "T", "source": "nse",
                                 "volume": 5, "delivery_pct": 40.0, "extra": 1})
        out = gw.proxy_quote("tcs")
        assert out == {"symbol": "TCS", "price": 101.5, "close": 100.0, "as_of": "T",
                       "source": "nse", "raw": {"volume": 5, "delivery_pct": 40.0}}
        assert md.urls == [f"{gw.MARKET_DATA_URL}/quote/TCS"] and md.kw[0]["timeout"] == 8
        assert fm.incs[0][0] == "stockky_quote_proxy_total"

    @pytest.mark.parametrize("payload,price", [
        ({"regularMarketPrice": 7}, 7), ({"close": 8}, 8), ({"last": 9}, 9),
        ({"price": 1, "regularMarketPrice": 2}, 1),
    ])
    def test_price_key_precedence(self, md, fm, payload, price):
        md.resp = FakeResp(200, payload)
        assert gw.proxy_quote("X")["price"] == price

    def test_defaults_for_missing_fields(self, md, fm):
        md.resp = FakeResp(200, {"price": 5})
        out = gw.proxy_quote("X")
        assert out["close"] == 5 and out["source"] == "market-data" and out["raw"] == {}
        assert out["as_of"].endswith("+05:30")            # IST-stamped

    def test_symbol_suffixes_stripped_and_uppercased(self, md, fm):
        gw.proxy_quote("reliance.ns")
        gw.proxy_quote("tcs.bo")
        assert md.urls[0].endswith("/quote/RELIANCE") and md.urls[1].endswith("/quote/TCS")

    def test_metrics_failure_is_swallowed(self, md, fm):
        fm.raise_on_inc = True
        assert gw.proxy_quote("X")["price"] == 101.5

    def test_market_data_200_without_a_price_falls_back_to_yfinance(self, md, fm, monkeypatch):
        # Fixed: a 200 with no usable price used to come back as price=None with yfinance never tried.
        md.resp = FakeResp(200, {"note": "empty"})
        self._yf(monkeypatch, {"last_price": 55.5})
        out = gw.proxy_quote("X")
        assert out["price"] == 55.5 and out["source"] == "yfinance_fast"

    def test_market_data_200_with_zero_price_also_falls_back(self, md, fm, monkeypatch):
        md.resp = FakeResp(200, {"price": 0})
        self._yf(monkeypatch, {"last_price": 12.0})
        assert gw.proxy_quote("X")["price"] == 12.0

    def _yf(self, monkeypatch, fast_info, ticker="X.NS"):
        monkeypatch.setattr(gw, "resolve_ns_ticker", lambda s: ticker)
        monkeypatch.setattr(gw.yf, "Ticker", lambda t: SimpleNamespace(fast_info=fast_info))

    def test_falls_back_to_yfinance_on_non_200(self, md, fm, monkeypatch):
        md.resp = FakeResp(503)
        self._yf(monkeypatch, {"last_price": 55.5})
        out = gw.proxy_quote("X")
        assert out["price"] == 55.5 and out["close"] == 55.5 and out["source"] == "yfinance_fast"

    def test_falls_back_on_exception_and_logs(self, md, fm, monkeypatch, logs):
        md.exc = httpx.ConnectError("down")
        self._yf(monkeypatch, {"lastPrice": 12.0})
        assert gw.proxy_quote("X")["price"] == 12.0
        assert any("quote proxy X" in m for m in logs["warning"])

    def test_yfinance_zero_or_garbage_price_becomes_none(self, md, fm, monkeypatch):
        md.resp = FakeResp(500)
        self._yf(monkeypatch, {"last_price": 0})
        assert gw.proxy_quote("X")["price"] is None
        self._yf(monkeypatch, {"last_price": "n/a"})
        assert gw.proxy_quote("X")["price"] is None

    def test_yfinance_ticker_without_fast_info(self, md, fm, monkeypatch):
        md.resp = FakeResp(500)
        monkeypatch.setattr(gw, "resolve_ns_ticker", lambda s: "X.NS")
        monkeypatch.setattr(gw.yf, "Ticker", lambda t: SimpleNamespace())
        assert gw.proxy_quote("X")["price"] is None

    def test_unresolvable_symbol_is_502(self, md, fm, monkeypatch):
        md.resp = FakeResp(500)
        monkeypatch.setattr(gw, "resolve_ns_ticker", lambda s: None)
        with pytest.raises(HTTPException) as e:
            gw.proxy_quote("ZZZ")
        assert e.value.status_code == 502 and "not resolvable on NSE" in e.value.detail

    def test_yfinance_exception_is_502(self, md, fm, monkeypatch):
        md.resp = FakeResp(500)
        monkeypatch.setattr(gw, "resolve_ns_ticker", lambda s: "X.NS")

        def boom(t):
            raise RuntimeError("yf down")

        monkeypatch.setattr(gw.yf, "Ticker", boom)
        with pytest.raises(HTTPException) as e:
            gw.proxy_quote("X")
        assert e.value.status_code == 502 and "yf down" in e.value.detail

    def test_over_http(self, md, fm, tc):
        assert tc.get("/quote/abc.ns").json()["symbol"] == "ABC"


# ── /circuits, rate limits, metrics ──────────────────────────────────────────

class TestCircuitsAndMetrics:
    def test_circuits_counts_open_and_sets_gauge(self, fm, monkeypatch):
        monkeypatch.setattr(gw, "all_snapshots", lambda: {
            "a": {"state": "open"}, "b": {"state": "closed"}, "c": {"state": "open"}, "d": {}})
        out = gw.circuits_status()
        assert set(out["circuits"]) == {"a", "b", "c", "d"}
        assert fm.gauges == [("stockky_circuits_open", 2.0)]

    def test_circuits_with_none_open(self, fm, monkeypatch):
        monkeypatch.setattr(gw, "all_snapshots", lambda: {})
        gw.circuits_status()
        assert fm.gauges == [("stockky_circuits_open", 0.0)]

    def test_rate_limits_passes_circuits_and_serves_three_paths(self, monkeypatch, tc):
        mon = FakeMonitor()
        monkeypatch.setattr(gw, "rate_limit_monitor", mon)
        monkeypatch.setattr(gw, "all_snapshots", lambda: {"x": {"state": "closed"}})
        out = _run(gw.ops_rate_limits())
        assert out["circuits"] == {"x": {"state": "closed"}}
        for p in ("/ops/rate-limits", "/api/rate-limits", "/api/ops/rate-limits"):
            assert tc.get(p).status_code == 200

    def test_event_records_with_coercions(self, monkeypatch, fm):
        mon = FakeMonitor()
        monkeypatch.setattr(gw, "rate_limit_monitor", mon)
        out = _run(gw.ops_rate_limits_event(
            {"source": "yahoo", "status": "429", "path": "/q", "detail": "slow", "symbol": "TCS"}))
        assert out == {"ok": True}
        assert mon.recorded == [dict(source="yahoo", status=429, path="/q", detail="slow", symbol="TCS")]
        assert fm.incs == [("rate_limit_events", {"source": "yahoo", "status": "429"})]

    def test_event_defaults_for_empty_payload(self, monkeypatch, fm):
        mon = FakeMonitor()
        monkeypatch.setattr(gw, "rate_limit_monitor", mon)
        assert _run(gw.ops_rate_limits_event({})) == {"ok": True}
        assert mon.recorded[0] == dict(source="unknown", status=0, path="", detail="", symbol="")
        assert fm.incs[0][1] == {"source": "unknown", "status": "0"}

    def test_event_bad_status_is_400(self, monkeypatch, fm):
        monkeypatch.setattr(gw, "rate_limit_monitor", FakeMonitor())
        r = _run(gw.ops_rate_limits_event({"status": "abc"}))
        assert r.status_code == 400 and b'"ok":false' in r.body.replace(b" ", b"")

    def test_event_error_text_truncated_to_120(self, monkeypatch, fm):
        mon = FakeMonitor()
        mon.raise_on_record = True
        monkeypatch.setattr(gw, "rate_limit_monitor", mon)
        r = _run(gw.ops_rate_limits_event({"source": "y"}))
        assert r.status_code == 400 and b"bad record" in r.body

    def test_event_metrics_failure_still_reports_ok_because_it_recorded(self, monkeypatch, fm):
        # FIXED: a metrics error AFTER the event was stored no longer answers 400.
        mon = FakeMonitor()
        monkeypatch.setattr(gw, "rate_limit_monitor", mon)
        fm.raise_on_inc = True
        r = _run(gw.ops_rate_limits_event({"source": "y", "status": 429}))
        assert r == {"ok": True} and len(mon.recorded) == 1

    def test_event_over_http_rejects_non_object(self, tc):
        assert tc.post("/ops/rate-limits/event", json=[1, 2]).status_code == 422

    def test_metrics_json_default_and_prom_variants(self, fm, tc):
        fm.snap = {"counters": {"a": 1}}
        assert tc.get("/metrics").json() == {"counters": {"a": 1}}
        for fmt in ("prom", "prometheus", "text", "PROM"):
            r = tc.get("/metrics", params={"format": fmt})
            assert r.text.startswith("# prom text")
            assert r.headers["content-type"].startswith("text/plain")
        assert tc.get("/metrics", params={"format": "weird"}).json() == {"counters": {"a": 1}}


# ── /ops/check-alert ─────────────────────────────────────────────────────────

class TestCheckAlert:
    @pytest.fixture
    def env(self, monkeypatch, fm, client):
        e = SimpleNamespace(snaps={}, fm=fm, client=client, now=1000.0)
        monkeypatch.setattr(gw, "all_snapshots", lambda: e.snaps)
        # Fresh cool-down state + controllable clock per test (state is module-level).
        monkeypatch.setattr(gw, "_OPS_ALERT_STATE", {})
        monkeypatch.setattr(gw, "_ops_alert_clock", lambda: e.now)
        monkeypatch.delenv("OPS_ALERT_COOLDOWN_SEC", raising=False)
        client.default = FakeResp(200, {"delivered": True})
        return e

    def test_healthy_returns_ok_without_notifying(self, env):
        out = _run(gw.ops_check_alert())
        assert out == {"alerted": False, "ok": True, "open_circuits": [], "error_rate": 0.0}
        assert env.client.calls == [] and env.fm.incs == []

    def test_open_circuit_alerts_and_delivers(self, env):
        env.snaps = {"market-data": {"state": "open"}, "decision": {"state": "closed"}, "news": {"state": "open"}}
        out = _run(gw.ops_check_alert())
        assert out["alerted"] is True and out["delivered"] is True
        assert out["open_circuits"] == ["market-data", "news"]
        assert out["problems"] == ["Open circuits: market-data, news"]
        (m, url, kw), = env.client.calls
        assert m == "POST" and url == f"{gw.NOTIFICATION_URL.rstrip('/')}/notify"
        assert kw["json"]["channel"] == "all" and kw["json"]["urgency"] == "high"
        assert kw["json"]["title"] == "⚠️ Stockky ops alert"
        assert env.fm.incs == [("stockky_ops_alerts_total", {})]

    @pytest.mark.parametrize("errors,oks,alerts", [
        (7, 12, False),    # total 19 -> below the volume floor even at 37%
        (8, 12, True),     # total 20, exactly 40%
        (7, 13, False),    # total 20, 35%
        (19, 1, True),
        (0, 50, False),
    ])
    def test_error_rate_threshold_boundaries(self, env, errors, oks, alerts):
        env.fm.snap = {"counters": {"x_dependency_errors": errors, "x_dependency_ok": oks}}
        assert _run(gw.ops_check_alert())["alerted"] is alerts

    def test_only_dependency_counters_are_summed_and_labels_included(self, env):
        env.fm.snap = {"counters": {
            'a_dependency_errors{svc="md"}': 6, 'b_dependency_errors{svc="ta"}': 4,
            "c_dependency_ok": 10, "requests_total": 9999, "x_errors": 500}}
        out = _run(gw.ops_check_alert())
        assert out["alerted"] is True and out["error_rate"] == pytest.approx(0.5)
        assert out["problems"] == ["Dependency error rate 50% (10/20)"]

    def test_both_problems_joined(self, env):
        env.snaps = {"a": {"state": "open"}}
        env.fm.snap = {"counters": {"e_dependency_errors": 30, "o_dependency_ok": 10}}
        out = _run(gw.ops_check_alert())
        assert len(out["problems"]) == 2
        assert " | " in env.client.calls[0][2]["json"]["message"]

    def test_missing_counters_key(self, env):
        env.fm.snap = {}
        assert _run(gw.ops_check_alert())["ok"] is True

    def test_notify_failure_keeps_alerted_true_and_logs(self, env, logs):
        env.snaps = {"a": {"state": "open"}}
        env.client.default = httpx.ConnectError("nope")
        out = _run(gw.ops_check_alert())
        assert out["alerted"] is True and out["delivered"] is False
        assert any("ops alert notify failed" in m for m in logs["warning"])

    @pytest.mark.parametrize("resp", [FakeResp(500, {"delivered": True}), FakeResp(200, {}),
                                      FakeResp(200, {"delivered": False}), FakeResp(200, None)])
    def test_delivered_requires_200_and_truthy_flag(self, env, resp):
        env.snaps = {"a": {"state": "open"}}
        env.client.default = resp
        assert _run(gw.ops_check_alert())["delivered"] is False

    def test_message_capped_at_1500_chars(self, env):
        env.snaps = {("n" * 400 + str(i)): {"state": "open"} for i in range(6)}
        _run(gw.ops_check_alert())
        assert len(env.client.calls[0][2]["json"]["message"]) == 1500

    def test_repeat_of_same_problem_is_suppressed_during_cooldown(self, env):
        env.snaps = {"a": {"state": "open"}}
        assert _run(gw.ops_check_alert())["alerted"] is True
        env.now += 60
        out = _run(gw.ops_check_alert())
        assert out["alerted"] is False and out["suppressed"] is True
        assert out["cooldown_remaining_sec"] == 840
        assert out["problems"] == ["Open circuits: a"] and out["open_circuits"] == ["a"]
        assert len(env.client.urls("POST")) == 1
        assert env.fm.incs == [("stockky_ops_alerts_total", {})]

    def test_alerts_again_once_cooldown_has_elapsed(self, env):
        env.snaps = {"a": {"state": "open"}}
        _run(gw.ops_check_alert())
        env.now += 900
        assert _run(gw.ops_check_alert())["alerted"] is True
        assert len(env.client.urls("POST")) == 2

    def test_new_problem_alerts_immediately_inside_cooldown(self, env):
        env.snaps = {"a": {"state": "open"}}
        _run(gw.ops_check_alert())
        env.now += 30
        env.snaps = {"a": {"state": "open"}, "b": {"state": "open"}}
        assert _run(gw.ops_check_alert())["alerted"] is True
        assert len(env.client.urls("POST")) == 2

    def test_recovery_resets_cooldown_so_a_recurrence_alerts(self, env):
        env.snaps = {"a": {"state": "open"}}
        _run(gw.ops_check_alert())
        env.now += 30
        env.snaps = {}
        assert _run(gw.ops_check_alert())["ok"] is True
        env.now += 30
        env.snaps = {"a": {"state": "open"}}
        assert _run(gw.ops_check_alert())["alerted"] is True
        assert len(env.client.urls("POST")) == 2

    def test_failed_delivery_is_not_suppressed_so_it_retries(self, env):
        env.snaps = {"a": {"state": "open"}}
        env.client.default = FakeResp(500, {})
        assert _run(gw.ops_check_alert())["delivered"] is False
        env.now += 60
        assert _run(gw.ops_check_alert())["alerted"] is True
        assert len(env.client.urls("POST")) == 2

    def test_error_rate_text_changing_does_not_defeat_cooldown(self, env):
        env.fm.snap = {"counters": {"x_dependency_errors": 10, "x_dependency_ok": 10}}
        _run(gw.ops_check_alert())
        env.now += 60
        env.fm.snap = {"counters": {"x_dependency_errors": 12, "x_dependency_ok": 10}}
        assert _run(gw.ops_check_alert())["suppressed"] is True
        assert len(env.client.urls("POST")) == 1

    @pytest.mark.parametrize("val, expect", [("0", 2), ("-5", 2), ("30", 1), ("abc", 1)])
    def test_cooldown_env_var(self, env, monkeypatch, val, expect):
        # 0 / negative disables suppression; a bad value falls back to the 900s default.
        monkeypatch.setenv("OPS_ALERT_COOLDOWN_SEC", val)
        env.snaps = {"a": {"state": "open"}}
        _run(gw.ops_check_alert())
        env.now += 60 if val != "30" else 29
        _run(gw.ops_check_alert())
        assert len(env.client.urls("POST")) == expect


# ── /ops/refresh-static-params ───────────────────────────────────────────────

class TestRefreshStaticParams:
    @pytest.fixture
    def env(self, monkeypatch, client, fast_sleep):
        e = SimpleNamespace(universe=["AAA", "BBB"], asked=[], redis=FakeRedis(),
                            fund={}, ev={}, news={}, calls=[], sleeps=fast_sleep)
        monkeypatch.setattr(gw, "_build_scan_universe", lambda: e.universe)

        async def fund(base, cl):
            e.calls.append(("fund", base))
            v = e.fund.get(base, ({"m": 1}, False))
            if isinstance(v, Exception):
                raise v
            return v

        async def ev(base, cl):
            e.calls.append(("ev", base))
            return e.ev.get(base, {"e": 1})

        async def news(base, cl):
            e.calls.append(("news", base))
            return e.news.get(base, {"n": 1})

        monkeypatch.setattr(gw, "_fetch_fundamental_cached", fund)
        monkeypatch.setattr(gw, "_fetch_events_cached", ev)
        monkeypatch.setattr(gw, "_fetch_news_cached", news)
        monkeypatch.setattr(gw, "_redis", e.redis)
        return e

    def test_happy_path_counts_and_payload(self, env):
        out = _run(gw.ops_refresh_static_params())
        assert out["status"] == "ok" and out["universe_size"] == 2
        assert out["refreshed"] == {"fundamental": 2, "event": 2, "news": 2, "errors": 0}
        assert out["ttl_seconds"] == gw.STATIC_PARAM_TTL and out["at"].endswith("+05:30")
        assert env.sleeps == [0.25, 0.25]

    def test_clears_the_three_cache_keys_per_symbol_then_hot_stocks(self, env):
        _run(gw.ops_refresh_static_params())
        assert env.redis.deleted == [
            f"{gw.FUNDAMENTAL_CACHE_PREFIX}AAA", f"{gw.EVENT_CACHE_PREFIX}AAA", f"{gw.NEWS_CACHE_PREFIX}AAA",
            f"{gw.FUNDAMENTAL_CACHE_PREFIX}BBB", f"{gw.EVENT_CACHE_PREFIX}BBB", f"{gw.NEWS_CACHE_PREFIX}BBB",
            gw.HOT_STOCKS_CACHE_KEY]

    @pytest.mark.parametrize("limit,expected", [(5, 10), (10, 10), (25, 25), (60, 60), (500, 80), (-3, 10)])
    def test_limit_is_clamped_to_10_80(self, env, limit, expected):
        env.universe = [f"S{i}" for i in range(200)]
        out = _run(gw.ops_refresh_static_params(limit=limit))
        assert out["universe_size"] == expected

    def test_default_limit_is_60(self, env):
        env.universe = [f"S{i}" for i in range(200)]
        assert _run(gw.ops_refresh_static_params())["universe_size"] == 60

    def test_symbols_normalised_and_blanks_skipped_without_sleeping(self, env):
        env.universe = ["  tcs.ns ", "", None, "infy.bo"]
        out = _run(gw.ops_refresh_static_params())
        assert [c[1] for c in env.calls if c[0] == "fund"] == ["TCS", "INFY"]
        assert out["universe_size"] == 4                      # blanks still counted in universe_size
        assert env.sleeps == [0.25, 0.25]

    def test_none_results_are_not_counted(self, env):
        env.fund["AAA"] = (None, True)
        env.ev["AAA"] = None
        env.news["BBB"] = None
        out = _run(gw.ops_refresh_static_params())
        assert out["refreshed"] == {"fundamental": 1, "event": 1, "news": 1, "errors": 0}

    def test_one_symbol_raising_is_counted_and_the_loop_continues(self, env, logs):
        env.fund["AAA"] = RuntimeError("upstream")
        out = _run(gw.ops_refresh_static_params())
        assert out["refreshed"] == {"fundamental": 1, "event": 1, "news": 1, "errors": 1}
        assert any("refresh-static-params AAA" in m for m in logs["warning"])
        assert env.sleeps == [0.25, 0.25]                     # the pacing sleep still runs after an error

    def test_redis_delete_failures_do_not_stop_the_refresh(self, env):
        env.redis.raise_on = {"*"}
        out = _run(gw.ops_refresh_static_params())
        assert out["refreshed"]["fundamental"] == 2 and out["refreshed"]["errors"] == 0

    def test_works_without_redis(self, env, monkeypatch):
        monkeypatch.setattr(gw, "_redis", None)
        out = _run(gw.ops_refresh_static_params())
        assert out["refreshed"]["fundamental"] == 2

    def test_over_http_query_param(self, env, tc):
        env.universe = [f"S{i}" for i in range(100)]
        assert tc.post("/ops/refresh-static-params", params={"limit": 15}).json()["universe_size"] == 15


# ── /market/history/{symbol} ─────────────────────────────────────────────────

def _MD_HIST():
    # market-data history URL prefix; a bare "/history/" would also match the training URL
    return f"{gw.MARKET_DATA_URL}/history/"


def _candles(*closes):
    return [{"date": f"2026-01-{i + 1:02d}", "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 10 * (i + 1)}
            for i, c in enumerate(closes)]


class TestMarketHistory:
    def test_market_data_success_builds_points_and_change(self, client):
        client.routes[_MD_HIST()] = FakeResp(200, {"candles": _candles(100, 110, 121)})
        out = _run(gw.market_history("tcs.ns", "1y"))
        assert out["symbol"] == "TCS" and out["period"] == "1y" and out["source"] == "market-data"
        assert out["change_pct"] == 21.0 and len(out["points"]) == 3
        assert out["points"][0] == {"date": "2026-01-01", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10}

    def test_warm_ping_then_history_with_mapped_period_and_timeouts(self, client):
        client.routes["/health"] = FakeResp(200)
        client.routes[_MD_HIST()] = FakeResp(200, {"candles": _candles(1, 2)})
        _run(gw.market_history("X", "3mo"))
        (_, u1, k1), (_, u2, k2) = client.calls
        assert u1.endswith("/health") and k1["params"] == {"warm": "true"}
        assert u2.endswith("/history/X") and k2["params"] == {"period": "3mo", "interval": "1d"}
        assert k1["timeout"].connect == 5.0 and k2["timeout"].read == 10.0

    @pytest.mark.parametrize("period,mapped", [("1d", "1mo"), ("5d", "1mo"), ("1mo", "1mo"), ("3mo", "3mo"),
                                               ("6mo", "6mo"), ("1y", "1y"), ("5y", "5y"), ("max", "1mo"), ("", "1mo")])
    def test_period_mapping_for_market_data(self, client, period, mapped):
        client.routes[_MD_HIST()] = FakeResp(200, {"candles": _candles(1, 2)})
        _run(gw.market_history("X", period))
        assert client.calls[-1][2]["params"]["period"] == mapped

    def test_1d_trims_to_the_last_two_sessions(self, client):
        # Fixed: "1d" used to return the whole 1-month window with change_pct spanning the month.
        client.routes[_MD_HIST()] = FakeResp(200, {"candles": _candles(*range(10, 40))})
        out = _run(gw.market_history("X", "1d"))
        assert [p["close"] for p in out["points"]] == [38, 39]
        assert out["period"] == "1d" and out["change_pct"] == 2.63

    def test_5d_trims_to_the_last_five_sessions(self, client):
        client.routes[_MD_HIST()] = FakeResp(200, {"candles": _candles(*range(10, 40))})
        out = _run(gw.market_history("X", "5d"))
        assert [p["close"] for p in out["points"]] == [35, 36, 37, 38, 39]
        assert out["change_pct"] == 11.43

    def test_1d_with_a_single_candle_is_left_alone(self, client):
        client.routes[_MD_HIST()] = FakeResp(200, {"candles": _candles(50)})
        out = _run(gw.market_history("X", "1d"))
        assert len(out["points"]) == 1 and out["change_pct"] == 0.0

    def test_1mo_is_not_trimmed(self, client):
        client.routes[_MD_HIST()] = FakeResp(200, {"candles": _candles(*range(10, 40))})
        assert len(_run(gw.market_history("X", "1mo"))["points"]) == 30

    def test_none_close_candles_skipped_and_volume_defaults_to_zero(self, client):
        cs = _candles(100, 105)
        cs.insert(1, {"date": "d", "close": None})
        cs[0]["volume"] = None
        client.routes[_MD_HIST()] = FakeResp(200, {"candles": cs})
        out = _run(gw.market_history("X"))
        assert [p["close"] for p in out["points"]] == [100, 105] and out["points"][0]["volume"] == 0

    def test_zero_first_close_gives_null_change(self, client):
        client.routes[_MD_HIST()] = FakeResp(200, {"candles": _candles(0, 50)})
        assert _run(gw.market_history("X"))["change_pct"] is None

    def test_warm_ping_failure_is_ignored(self, client):
        client.routes["/health"] = httpx.ReadTimeout("")
        client.routes[_MD_HIST()] = FakeResp(200, {"candles": _candles(1, 2)})
        assert _run(gw.market_history("X"))["source"] == "market-data"

    def test_empty_candles_fall_through_to_training_with_status_in_error(self, client):
        client.routes["/health"] = FakeResp(200)
        client.routes[_MD_HIST()] = FakeResp(200, {"candles": []})
        client.routes["/api/stock/history/"] = FakeResp(200, {"points": [1]})
        out = _run(gw.market_history("X", "1mo"))
        assert out == {"points": [1], "source": "training"}

    def test_training_source_is_kept_when_it_supplies_one(self, client):
        client.routes[_MD_HIST()] = FakeResp(404)
        client.routes["/api/stock/history/"] = FakeResp(200, {"points": [], "source": "yf"})
        assert _run(gw.market_history("X"))["source"] == "yf"

    @pytest.mark.parametrize("period,sent", [("1d", "1d"), ("5d", "5d"), ("1mo", "1mo"), ("1y", "1y"),
                                             ("5y", "5y"), ("3mo", "1mo"), ("6mo", "1mo"), ("zzz", "1mo")])
    def test_training_period_passthrough_is_narrower_than_market_data(self, client, period, sent):
        client.routes[_MD_HIST()] = FakeResp(404)
        client.routes["/api/stock/history/"] = FakeResp(200, {})
        _run(gw.market_history("X", period))
        assert client.calls[-1][2]["params"] == {"period": sent}

    def test_both_sources_failing_is_503_with_both_reasons(self, client):
        client.routes[_MD_HIST()] = FakeResp(404)
        client.routes["/api/stock/history/"] = FakeResp(502)
        with pytest.raises(HTTPException) as e:
            _run(gw.market_history("X"))
        assert e.value.status_code == 503
        assert e.value.detail == "Chart unavailable for X: market-data HTTP 404; training HTTP 502"

    def test_empty_message_exceptions_keep_their_class_name(self, client):
        client.routes["/health"] = FakeResp(200)
        client.routes[_MD_HIST()] = httpx.ReadTimeout("")
        client.routes["/api/stock/history/"] = httpx.ConnectTimeout("")
        with pytest.raises(HTTPException) as e:
            _run(gw.market_history("X"))
        assert e.value.detail == "Chart unavailable for X: ReadTimeout; training ConnectTimeout"

    def test_exceptions_with_a_message_show_it(self, client):
        client.routes[_MD_HIST()] = RuntimeError("md boom")
        client.routes["/api/stock/history/"] = RuntimeError("tr boom")
        with pytest.raises(HTTPException) as e:
            _run(gw.market_history("X"))
        assert e.value.detail == "Chart unavailable for X: RuntimeError: md boom; training RuntimeError: tr boom"

    def test_training_non_object_json_is_reported_not_crashed(self, client):
        client.routes[_MD_HIST()] = FakeResp(404)
        client.routes["/api/stock/history/"] = FakeResp(200, [1, 2, 3])      # list has no .get
        with pytest.raises(HTTPException) as e:
            _run(gw.market_history("X"))
        assert e.value.status_code == 503 and "training AttributeError" in e.value.detail

    def test_first_candle_null_close_uses_next_valid_for_baseline(self, client):
        cs = [{"date": "a", "close": None}] + _candles(200, 220)
        client.routes[_MD_HIST()] = FakeResp(200, {"candles": cs})
        assert _run(gw.market_history("X"))["change_pct"] == 10.0

    def test_over_http_503(self, client, tc):
        client.default = FakeResp(500)
        r = tc.get("/market/history/abc")
        assert r.status_code == 503 and "Chart unavailable for ABC" in r.json()["detail"]


# ── /ops/db-status, neon keep-alive, wake-db-all ─────────────────────────────

class TestDbStatus:
    def test_ok_passes_selected_keys_and_nulls_for_missing_ones(self, client):
        client.routes["/health"] = FakeResp(200, {"db_backend": "neon", "db_connected": True, "status": "ok", "junk": 1})
        out = _run(gw.ops_db_status())
        assert out["ok"] is True and out["source"] == "training"
        assert out["db_backend"] == "neon" and out["db_connected"] is True and out["status"] == "ok"
        assert out["db_durable"] is None and out["db_error"] is None and "junk" not in out
        assert client.urls()[0] == f"{gw.TRAINING_URL.rstrip('/')}/health"

    def test_non_200_reports_the_status(self, client):
        client.routes["/health"] = FakeResp(503)
        out = _run(gw.ops_db_status())
        assert out == {"ok": False, "db_connected": False,
                       "db_message": "Training service health HTTP 503", "db_error": "Training service health HTTP 503"}

    def test_unreachable(self, client):
        client.routes["/health"] = httpx.ConnectError("e" * 300)
        out = _run(gw.ops_db_status())
        assert out["ok"] is False and out["db_backend"] == "unknown" and out["db_durable"] is False
        assert out["db_message"].startswith("Cannot reach training service") and len(out["db_error"]) == 200

    def test_a_200_with_bad_json_is_reported_as_such_not_as_unreachable(self, client):
        # FIXED: the service answered, so the message no longer says "Cannot reach".
        client.routes["/health"] = FakeResp(200, json_raises=True)
        out = _run(gw.ops_db_status())
        assert out["ok"] is False and out["db_connected"] is False
        assert "Cannot reach" not in out["db_message"] and "not with JSON" in out["db_message"]


class TestNeonKeepalive:
    def test_kv_cache_not_loaded(self, monkeypatch):
        monkeypatch.setattr(gw, "_kv_cache", None)
        assert gw._neon_keepalive_ping() == {"ok": False, "neon_connected": False, "error": "kv_cache not loaded"}

    def test_status_connected(self, monkeypatch):
        monkeypatch.setattr(gw, "_kv_cache", SimpleNamespace(status=lambda: {"neon_connected": True}))
        assert gw._neon_keepalive_ping() == {"ok": True, "neon_connected": True, "error": None}

    def test_status_disconnected_carries_the_error(self, monkeypatch):
        monkeypatch.setattr(gw, "_kv_cache", SimpleNamespace(status=lambda: {"neon_connected": False, "neon_error": "ssl"}))
        assert gw._neon_keepalive_ping() == {"ok": False, "neon_connected": False, "error": "ssl"}

    def test_status_returning_none_is_treated_as_disconnected(self, monkeypatch):
        monkeypatch.setattr(gw, "_kv_cache", SimpleNamespace(status=lambda: None))
        assert gw._neon_keepalive_ping()["ok"] is False

    def test_module_without_status_falls_back_to_a_read(self, monkeypatch):
        seen = []
        monkeypatch.setattr(gw, "_kv_cache", SimpleNamespace(get=lambda k: seen.append(k)))
        assert gw._neon_keepalive_ping() == {"ok": True, "neon_connected": True, "error": None}
        assert seen == ["__neon_keepalive__"]

    def test_exceptions_are_captured_and_truncated(self, monkeypatch):
        def boom():
            raise RuntimeError("x" * 500)

        monkeypatch.setattr(gw, "_kv_cache", SimpleNamespace(status=boom))
        out = gw._neon_keepalive_ping()
        assert out["ok"] is False and len(out["error"]) == 200

    def test_connected_status_also_issues_a_real_read(self, monkeypatch):
        # Fixed: status() only reports a cached flag, so the ping now also reads a key to touch Neon.
        called = []
        monkeypatch.setattr(gw, "_kv_cache", SimpleNamespace(status=lambda: {"neon_connected": True},
                                                            get=lambda k: called.append(k)))
        out = gw._neon_keepalive_ping()
        assert called == ["__neon_keepalive__"] and out["ok"] is True

    def test_disconnected_status_skips_the_read(self, monkeypatch):
        called = []
        monkeypatch.setattr(gw, "_kv_cache", SimpleNamespace(status=lambda: {"neon_connected": False},
                                                            get=lambda k: called.append(k)))
        assert gw._neon_keepalive_ping()["ok"] is False and called == []

    def test_a_failing_read_marks_the_ping_not_ok(self, monkeypatch):
        def boom(k):
            raise RuntimeError("y" * 500)

        monkeypatch.setattr(gw, "_kv_cache", SimpleNamespace(status=lambda: {"neon_connected": True}, get=boom))
        out = gw._neon_keepalive_ping()
        assert out["ok"] is False and out["neon_connected"] is True and len(out["error"]) == 200

    def test_route_serves_get_and_post_with_hint(self, monkeypatch, tc):
        monkeypatch.setattr(gw, "_kv_cache", SimpleNamespace(status=lambda: {"neon_connected": True}))
        for method in (tc.get, tc.post):
            j = method("/ops/neon-keepalive").json()
            assert j["ok"] is True and j["at"].endswith("+05:30") and "4 minutes" in j["hint"]


class TestWakeDbAll:
    @pytest.fixture
    def env(self, monkeypatch, client):
        monkeypatch.setattr(gw, "_neon_keepalive_ping", lambda: {"ok": True, "neon_connected": True, "error": None})
        client.routes["/health"] = FakeResp(200, {"db_connected": True, "db_backend": "neon"})
        return client

    def test_all_ok(self, env):
        out = _run(gw.ops_wake_db_all())
        assert out["ok"] is True and out["at"].endswith("+05:30")
        assert out["targets"]["training_db"] == {"ok": True, "db_connected": True, "db_backend": "neon"}
        assert out["targets"]["gateway_neon"]["ok"] is True
        (_, url, kw), = env.calls
        assert url == f"{gw.TRAINING_URL.rstrip('/')}/health" and kw["params"] == {"warm": "true"} and kw["timeout"] == 15.0

    def test_gateway_ping_exception_is_contained(self, env, monkeypatch):
        def boom():
            raise RuntimeError("g" * 300)

        monkeypatch.setattr(gw, "_neon_keepalive_ping", boom)
        out = _run(gw.ops_wake_db_all())
        assert out["ok"] is False and len(out["targets"]["gateway_neon"]["error"]) == 200
        assert out["targets"]["training_db"]["ok"] is True

    def test_training_non_200(self, env):
        env.routes["/health"] = FakeResp(500, {"db_connected": True})
        t = _run(gw.ops_wake_db_all())["targets"]["training_db"]
        assert t == {"ok": False, "db_connected": None, "db_backend": None}

    def test_training_json_failure_still_ok_with_nulls(self, env):
        env.routes["/health"] = FakeResp(200, json_raises=True)
        out = _run(gw.ops_wake_db_all())
        assert out["targets"]["training_db"] == {"ok": True, "db_connected": None, "db_backend": None}

    def test_training_reporting_db_down_turns_the_summary_red(self, env):
        # FIXED: HTTP 200 with db_connected=False is no longer "ok".
        env.routes["/health"] = FakeResp(200, {"db_connected": False, "db_backend": "none"})
        out = _run(gw.ops_wake_db_all())
        assert out["ok"] is False and out["targets"]["training_db"]["ok"] is False
        assert out["targets"]["training_db"]["db_connected"] is False

    def test_training_url_not_configured(self, env, monkeypatch):
        monkeypatch.setattr(gw, "TRAINING_URL", "")
        out = _run(gw.ops_wake_db_all())
        assert out["ok"] is False
        assert out["targets"]["training_db"] == {"ok": False, "error": "TRAINING_URL not configured"}
        assert env.calls == []

    def test_training_request_exception(self, env):
        env.routes["/health"] = httpx.ConnectError("dead")
        out = _run(gw.ops_wake_db_all())
        assert out["ok"] is False and "dead" in out["targets"]["training_db"]["error"]

    def test_served_on_get_and_post(self, env, tc):
        assert tc.get("/ops/wake-db-all").json()["ok"] is True
        assert tc.post("/ops/wake-db-all").json()["ok"] is True


# ── /ops/idle-tick ───────────────────────────────────────────────────────────

class TestIdleTick:
    @pytest.fixture
    def env(self, monkeypatch, logs):
        e = SimpleNamespace(phase="open", neon={"ok": True}, neon_exc=None, idx_exc=None, idx_calls=[],
                            cached="hot", hot_calls=[], hot_exc=None, redis_exc=None)
        monkeypatch.setattr(gw, "_market_session_phase_ist", lambda: e.phase)

        def neon():
            if e.neon_exc:
                raise e.neon_exc
            return e.neon

        def idx(force_refresh=False):
            e.idx_calls.append(force_refresh)
            if e.idx_exc:
                raise e.idx_exc

        def rget(key):
            if e.redis_exc:
                raise e.redis_exc
            return e.cached

        async def hot(force=False, **kw):
            e.hot_calls.append(force)
            if e.hot_exc:
                raise e.hot_exc

        monkeypatch.setattr(gw, "_neon_keepalive_ping", neon)
        monkeypatch.setattr(gw, "get_market_indices", idx)
        monkeypatch.setattr(gw, "_redis_get", rget)
        monkeypatch.setattr(gw, "stockky_hot_stocks", hot)
        return e

    @pytest.mark.parametrize("phase", ["closed", "weekend", "holiday", "", "premarket"])
    def test_off_market_is_a_noop(self, env, phase):
        env.phase = phase
        out = _run(gw.ops_idle_tick())
        assert out["ran"] is False and out["reason"] == "off_market" and out["phase"] == phase
        assert env.idx_calls == [] and env.hot_calls == []

    @pytest.mark.parametrize("phase", ["preopen", "open", "post"])
    def test_market_window_phases_run(self, env, phase):
        env.phase = phase
        out = _run(gw.ops_idle_tick())
        assert out["ran"] is True and out["phase"] == phase
        assert out["actions"] == ["neon_keepalive_ok", "indices", "hot_stocks_cached"]
        assert env.idx_calls == [False] and env.hot_calls == []

    def test_hot_stocks_rebuilt_only_on_a_cache_miss(self, env):
        env.cached = None
        out = _run(gw.ops_idle_tick())
        assert out["actions"][-1] == "hot_stocks_miss" and env.hot_calls == [False]

    def test_neon_reports_not_ok(self, env):
        env.neon = {"ok": False}
        assert _run(gw.ops_idle_tick())["actions"][0] == "neon_keepalive_error"

    def test_neon_raising_is_logged_and_recorded(self, env, logs):
        env.neon_exc = RuntimeError("neon x")
        out = _run(gw.ops_idle_tick())
        assert out["actions"][0] == "neon_keepalive_error"
        assert any("neon keepalive" in m for m in logs["debug"])

    def test_indices_failure_is_silent_in_actions(self, env, logs):
        env.idx_exc = RuntimeError("yf")
        out = _run(gw.ops_idle_tick())
        assert "indices" not in out["actions"] and any("idle-tick indices" in m for m in logs["debug"])

    def test_hot_stocks_failure_is_contained(self, env, logs):
        env.cached, env.hot_exc = None, RuntimeError("scan")
        out = _run(gw.ops_idle_tick())
        assert out["ran"] is True and "hot_stocks_miss" not in out["actions"]
        assert any("idle-tick hot" in m for m in logs["debug"])

    def test_redis_read_failure_is_contained(self, env):
        env.redis_exc = RuntimeError("redis")
        assert _run(gw.ops_idle_tick())["ran"] is True

    def test_over_http(self, env, tc):
        env.phase = "closed"
        assert tc.post("/ops/idle-tick").json()["ran"] is False


# ── /ops/qstash/tick and /ops/qstash/publish ─────────────────────────────────

class FakeQstash:
    def __init__(self):
        self.verify_calls, self.verify_ok, self.verify_exc = [], True, None
        self._enabled = True
        self.published, self.scheduled = [], []

    def verify_signature(self, sig, body, expected_url=None):
        self.verify_calls.append((sig, body, expected_url))
        if self.verify_exc:
            raise self.verify_exc
        return self.verify_ok

    def enabled(self):
        return self._enabled

    def publish(self, dest, payload, delay_seconds=0):
        self.published.append((dest, payload, delay_seconds))
        return {"ok": True, "published": dest}

    def schedule_gateway_tick(self, delay_seconds=0, body=None):
        self.scheduled.append((delay_seconds, body))
        return {"ok": True, "scheduled": delay_seconds}


class TestQstashTick:
    @pytest.fixture
    def env(self, monkeypatch, client):
        q = FakeQstash()
        monkeypatch.setattr(gw, "qstash_client", q)
        monkeypatch.delenv("API_GATEWAY_URL", raising=False)
        calls = []

        async def keepalive(deep=False):
            calls.append(deep)

        monkeypatch.setattr(gw, "ops_keepalive", keepalive)
        return SimpleNamespace(q=q, calls=calls, client=client)

    def test_without_qstash_module_no_verification(self, env, monkeypatch, tc):
        monkeypatch.setattr(gw, "qstash_client", None)
        r = tc.post("/ops/qstash/tick")
        assert r.status_code == 200 and r.json()["source"] == "qstash"

    def test_valid_signature_passes_headers_body_and_expected_url(self, env, monkeypatch, tc):
        monkeypatch.setenv("API_GATEWAY_URL", "https://gw.example/")
        r = tc.post("/ops/qstash/tick", content=b'{"action":"noop"}', headers={"Upstash-Signature": "sig1"})
        assert r.status_code == 200
        assert env.q.verify_calls == [("sig1", b'{"action":"noop"}', "https://gw.example/ops/qstash/tick")]

    def test_expected_url_is_none_when_gateway_url_unset(self, env, tc):
        tc.post("/ops/qstash/tick")
        assert env.q.verify_calls[0][2] is None and env.q.verify_calls[0][0] == ""

    def test_bad_signature_is_401(self, env, tc):
        env.q.verify_ok = False
        r = tc.post("/ops/qstash/tick", headers={"upstash-signature": "bad"})
        assert r.status_code == 401 and r.json()["detail"] == "Invalid QStash signature"
        assert env.calls == []

    def test_verifier_crash_fails_closed(self, env, tc):
        # A non-HTTP error while verifying is no longer swallowed: the tick is refused (503)
        # and nothing runs. (qstash_client itself still accepts when PyJWT/keys are missing —
        # that is a deliberate, separate degraded-mode choice.)
        env.q.verify_exc = RuntimeError("jwt exploded")
        r = tc.post("/ops/qstash/tick", json={"action": "wake"})
        assert r.status_code == 503
        assert r.json()["detail"] == "QStash signature verification unavailable"
        assert env.calls == [] and env.client.calls == []

    def test_verifier_crash_is_logged(self, env, tc, caplog):
        env.q.verify_exc = RuntimeError("jwt exploded")
        with caplog.at_level("ERROR"):
            tc.post("/ops/qstash/tick")
        assert "signature verification crashed" in caplog.text

    def test_served_on_get_too(self, env, tc):
        assert tc.get("/ops/qstash/tick").json()["ok"] is True

    def test_plain_tick_makes_no_calls_and_no_longer_claims_to_warm_anything(self, env, tc):
        # The old "warm" list only named /health and /ops/keepalive without calling them;
        # the field is gone so the response doesn't claim work that never happened.
        j = tc.post("/ops/qstash/tick").json()
        assert j == {"ok": True, "source": "qstash"}
        assert env.client.calls == [] and env.calls == []

    @pytest.mark.parametrize("action", ["wake", "wake-all", "scan"])
    def test_wake_actions_run_a_shallow_keepalive(self, env, tc, action):
        j = tc.post("/ops/qstash/tick", json={"action": action}).json()
        assert j["keepalive"] is True and env.calls == [False]

    @pytest.mark.parametrize("body", [b"", b"not json", b'{"action":"other"}', b"{}"])
    def test_other_bodies_skip_keepalive(self, env, tc, body):
        j = tc.post("/ops/qstash/tick", content=body).json()
        assert "keepalive" not in j and env.calls == []

    def test_keepalive_failure_is_reported(self, env, monkeypatch, tc):
        async def bad(deep=False):
            raise RuntimeError("k" * 300)

        monkeypatch.setattr(gw, "ops_keepalive", bad)
        j = tc.post("/ops/qstash/tick", json={"action": "wake"}).json()
        assert j["ok"] is True and len(j["keepalive_error"]) == 120

    def test_json_array_body_lands_in_the_error_field(self, env, tc):
        j = tc.post("/ops/qstash/tick", json=[1, 2]).json()
        assert j["ok"] is True and "error" in j


class TestQstashPublish:
    @pytest.fixture
    def env(self, monkeypatch):
        q = FakeQstash()
        monkeypatch.setattr(gw, "qstash_client", q)
        return q

    def test_module_missing(self, monkeypatch, tc):
        monkeypatch.setattr(gw, "qstash_client", None)
        assert tc.post("/ops/qstash/publish", json={}).json() == {"ok": False, "error": "QStash not configured (set QSTASH_TOKEN)"}

    def test_not_enabled(self, env, tc):
        env._enabled = False
        assert tc.post("/ops/qstash/publish", json={}).json()["ok"] is False
        assert env.published == [] and env.scheduled == []

    def test_no_destination_schedules_a_gateway_tick(self, env, tc):
        j = tc.post("/ops/qstash/publish", json={"delay_seconds": "30", "x": 1}).json()
        assert j == {"ok": True, "scheduled": 30}
        assert env.scheduled == [(30, {"delay_seconds": "30", "x": 1})]

    def test_destination_and_payload(self, env, tc):
        j = tc.post("/ops/qstash/publish", json={"url": "https://a/b", "payload": {"k": 1}, "delay_seconds": 5}).json()
        assert j["published"] == "https://a/b" and env.published == [("https://a/b", {"k": 1}, 5)]

    def test_destination_alias_and_default_payload(self, env, tc):
        tc.post("/ops/qstash/publish", json={"destination": "https://c/d"})
        assert env.published == [("https://c/d", {}, 0)]

    def test_unparseable_body_is_treated_as_empty(self, env, tc):
        tc.post("/ops/qstash/publish", content=b"not json")
        assert env.scheduled == [(0, {})]

    def test_non_numeric_delay_is_a_400_with_a_message(self, env, tc):
        # FIXED: a bad delay no longer escapes as a bare 500.
        r = tc.post("/ops/qstash/publish", json={"delay_seconds": "soon"})
        assert r.status_code == 400 and r.json()["ok"] is False and "delay_seconds" in r.json()["error"]


# ── wake-all, momentum, health, system health ────────────────────────────────

class TestWakeAndHealth:
    def test_wake_all_ok_and_degraded(self, monkeypatch):
        async def ok(*a, **k):
            return {"a": {"ok": True}, "b": {"ok": False}}

        monkeypatch.setattr(gw, "_wake_required_services", ok)
        out = _run(gw.wake_all_services())
        assert out == {"status": "ok", "warmed_ok": 1, "total": 2, "services": {"a": {"ok": True}, "b": {"ok": False}}}

        async def none(*a, **k):
            return {"a": {"ok": False}}

        monkeypatch.setattr(gw, "_wake_required_services", none)
        assert _run(gw.wake_all_services())["status"] == "degraded"

    def test_wake_all_empty_results_are_degraded(self, monkeypatch):
        async def empty(*a, **k):
            return {}

        monkeypatch.setattr(gw, "_wake_required_services", empty)
        out = _run(gw.wake_all_services())
        assert out["status"] == "degraded" and out["total"] == 0

    def test_wake_all_served_on_get_and_post(self, monkeypatch, tc):
        async def ok(*a, **k):
            return {"a": {"ok": True}}

        monkeypatch.setattr(gw, "_wake_required_services", ok)
        assert tc.get("/wake-all").json()["status"] == "ok" and tc.post("/wake-all").json()["status"] == "ok"

    def test_momentum_movers_route(self, monkeypatch, logs):
        monkeypatch.setattr(gw, "_get_momentum_movers", lambda: ["A", "B"])
        assert gw.momentum_movers_route() == {"symbols": ["A", "B"], "count": 2}

    def test_momentum_movers_failure_returns_empty_and_logs(self, monkeypatch, logs):
        def boom():
            raise RuntimeError("nse 403")

        monkeypatch.setattr(gw, "_get_momentum_movers", boom)
        assert gw.momentum_movers_route() == {"symbols": [], "count": 0}
        assert any("momentum_movers_route failed" in m for m in logs["warning"])

    def test_health_reflects_redis_and_ignores_warm(self, monkeypatch):
        monkeypatch.setattr(gw, "_redis", None)
        assert gw.health() == {"status": "ok", "service": "api-gateway", "redis": False, "ready": True}
        monkeypatch.setattr(gw, "_redis", object())
        assert gw.health(warm=True)["redis"] is True

    def test_ready_is_503_without_redis_and_200_with_it(self, monkeypatch, tc):
        # FIXED: "not ready" is now an HTTP 503, so an orchestrator probing status codes sees it.
        monkeypatch.setattr(gw, "_redis", None)
        r = tc.get("/ready")
        assert r.status_code == 503 and r.json() == {"ready": False}
        monkeypatch.setattr(gw, "_redis", object())
        r = tc.get("/ready")
        assert r.status_code == 200 and r.json() == {"ready": True}


class TestSystemHealth:
    @pytest.fixture
    def env(self, monkeypatch, client):
        svc = {"market-data": {"url": "http://md", "required": True},
               "decision": {"url": "http://dc/", "required": True},
               "news": {"url": "http://nw", "required": False},
               "cfg": {"url": "", "required": False}}
        monkeypatch.setattr(gw, "SYSTEM_SERVICES", svc)
        client.routes["http://md"] = FakeResp(200, {})
        client.routes["http://dc"] = FakeResp(200, {"status": "healthy"})
        client.routes["http://nw"] = FakeResp(200, {})
        return client

    def test_all_up(self, env):
        out = _run(gw.system_health())
        assert out["required_ok"] is True and out["all_ok"] is False        # 'cfg' is not configured
        s = out["services"]
        assert s["api-gateway"] == {"ok": True, "required": True, "status": "up", "url": None}
        assert s["market-data"] == {"ok": True, "required": True, "status": "up", "url": "http://md"}
        assert s["decision"]["upstream_status"] == "healthy"
        assert s["cfg"] == {"ok": False, "required": False, "status": "not_configured", "url": None}

    def test_url_trailing_slash_is_trimmed_for_the_probe(self, env):
        _run(gw.system_health())
        assert "http://dc/health" in env.urls()

    def test_degraded_mounts_are_listed(self, env):
        env.routes["http://nw"] = FakeResp(200, {"failed": ["a", "b"], "mounts": {"a": "x"}})
        e = _run(gw.system_health())["services"]["news"]
        assert e["ok"] is False and e["status"] == "degraded"
        assert e["failed_mounts"] == ["a", "b"] and e["mounts"] == {"a": "x"}

    def test_mounts_without_failures_are_shown_but_up(self, env):
        env.routes["http://nw"] = FakeResp(200, {"mounts": {"a": "ok"}})
        e = _run(gw.system_health())["services"]["news"]
        assert e["ok"] is True and e["mounts"] == {"a": "ok"} and "failed_mounts" not in e

    def test_bad_json_or_empty_body_still_up(self, env):
        env.routes["http://nw"] = FakeResp(200, json_raises=True)
        env.routes["http://md"] = FakeResp(200, content=False)
        s = _run(gw.system_health())["services"]
        assert s["news"]["status"] == "up" and s["market-data"]["status"] == "up"

    def test_non_200_and_unreachable(self, env):
        env.routes["http://nw"] = FakeResp(503)
        env.routes["http://md"] = httpx.ConnectError("m" * 300)
        s = _run(gw.system_health())["services"]
        assert s["news"] == {"ok": False, "required": False, "status": "http_503", "url": "http://nw"}
        assert s["market-data"]["status"] == "unreachable" and len(s["market-data"]["error"]) == 100

    def test_required_service_down_flips_required_ok(self, env):
        env.routes["http://md"] = FakeResp(500)
        out = _run(gw.system_health())
        assert out["required_ok"] is False

    def test_optional_service_down_keeps_required_ok(self, env):
        env.routes["http://nw"] = FakeResp(500)
        assert _run(gw.system_health())["required_ok"] is True

    def test_non_object_body_is_reported_unreachable(self, env):
        env.routes["http://nw"] = FakeResp(200, [1])
        assert _run(gw.system_health())["services"]["news"]["status"] == "unreachable"

    def test_per_service_timeout_is_passed_to_each_probe(self, env):
        # FIXED: 15s for market-data, 10s for the rest — one hung service no longer holds the
        # whole health page for the shared client's 90s default.
        _run(gw.system_health())
        by_url = {url: kw.get("timeout") for _, url, kw in env.calls}
        assert by_url["http://md/health"] == 15 and by_url["http://dc/health"] == 10
        assert by_url["http://nw/health"] == 10

    def test_probes_are_concurrent_and_all_answer(self, env):
        _run(gw.system_health())
        assert sorted(env.urls()) == ["http://dc/health", "http://md/health", "http://nw/health"]


class TestWakeProbe:
    def test_sequential_probe_results(self, monkeypatch, client):
        monkeypatch.setattr(gw, "SYSTEM_SERVICES", {
            "a": {"url": "http://a", "required": True}, "b": {"url": "", "required": False},
            "c": {"url": "http://c", "required": False}, "d": {"url": "http://d", "required": False}})
        client.routes["http://a"] = FakeResp(200)
        client.routes["http://c"] = FakeResp(500)
        client.routes["http://d"] = httpx.ConnectError("gone")
        out = _run(gw.wake_all_services_probe())["results"]
        assert out["a"] == {"ok": True, "status": 200}
        assert out["b"] == {"ok": False, "error": "no url"}
        assert out["c"] == {"ok": False, "status": 500}
        assert out["d"]["ok"] is False and "gone" in out["d"]["error"]

    def test_trims_trailing_slash_like_the_other_probes(self, monkeypatch, client):
        # FIXED: a configured URL ending in "/" now probes "/health", not "//health".
        monkeypatch.setattr(gw, "SYSTEM_SERVICES", {"a": {"url": "http://a/", "required": True}})
        client.routes["http://a"] = FakeResp(200)
        _run(gw.wake_all_services_probe())
        assert client.urls() == ["http://a/health"]


# ── watchlist / events / searched ────────────────────────────────────────────

class TestWatchlist:
    @pytest.fixture
    def env(self, monkeypatch):
        e = SimpleNamespace(store=["AAA", "BBB"], saved=[], redis=FakeRedis())
        monkeypatch.setattr(gw, "_load_watchlist", lambda: list(e.store))

        def save(symbols):
            e.saved.append(list(symbols))
            e.store = list(symbols)

        monkeypatch.setattr(gw, "_save_watchlist", save)
        monkeypatch.setattr(gw, "_redis", e.redis)
        return e

    def test_get(self, env):
        assert gw.get_watchlist() == {"symbols": ["AAA", "BBB"]}

    def test_set_uppercases_strips_whitespace_and_busts_universe_cache(self, env):
        out = gw.set_watchlist(gw.WatchlistUpdate(symbols=[" tcs ", "infy"]))
        assert out == {"symbols": ["TCS", "INFY"]} and env.saved == [["TCS", "INFY"]]
        assert env.redis.deleted == [gw.SCAN_UNIVERSE_KEY]

    def test_set_response_matches_what_is_stored(self, env):
        # FIXED: the route strips suffixes and drops blanks itself, so the response is what is persisted.
        out = gw.set_watchlist(gw.WatchlistUpdate(symbols=["tcs.ns", "", "infy.bo"]))
        assert out["symbols"] == ["TCS", "INFY"]
        assert env.saved == [["TCS", "INFY"]]

    def test_real_save_cleans_what_the_route_echoes(self, monkeypatch):
        seen = {}
        monkeypatch.setitem(__import__("sys").modules, "kv_cache",
                            SimpleNamespace(watchlist_set=lambda clean: seen.update(clean=clean)))
        gw._save_watchlist(["TCS.NS", "", "infy.bo"])
        assert seen["clean"] == ["TCS", "INFY"]

    def test_set_and_add_tolerate_redis_delete_failure(self, env):
        env.redis.raise_on = {"*"}
        assert gw.set_watchlist(gw.WatchlistUpdate(symbols=["A"]))["symbols"] == ["A"]
        assert gw.add_to_watchlist(gw.WatchlistUpdate(symbols=["Z"]))["added"] == ["Z"]

    def test_set_without_redis(self, env, monkeypatch):
        monkeypatch.setattr(gw, "_redis", None)
        assert gw.set_watchlist(gw.WatchlistUpdate(symbols=["A"]))["symbols"] == ["A"]

    def test_add_new_existing_and_blank(self, env):
        out = gw.add_to_watchlist(gw.WatchlistUpdate(symbols=["ccc", "aaa.ns", "  ", "ddd.bo", "ccc"]))
        assert out["added"] == ["CCC", "DDD"] and out["already"] == ["AAA", "CCC"]
        assert out["symbols"] == ["AAA", "BBB", "CCC", "DDD"]        # sorted, de-duplicated
        assert out["message"] == "Already in watchlist: AAA, CCC"
        assert env.redis.deleted == [gw.SCAN_UNIVERSE_KEY]

    def test_add_only_existing_message(self, env):
        out = gw.add_to_watchlist(gw.WatchlistUpdate(symbols=["aaa"]))
        assert out["message"] == "Already in watchlist" and out["added"] == []

    def test_add_only_new_has_no_message(self, env):
        assert gw.add_to_watchlist(gw.WatchlistUpdate(symbols=["zzz"]))["message"] is None

    def test_add_nothing_still_saves_and_clears_cache(self, env):
        out = gw.add_to_watchlist(gw.WatchlistUpdate(symbols=[]))
        assert out["message"] is None and env.saved == [["AAA", "BBB"]] and env.redis.deleted == [gw.SCAN_UNIVERSE_KEY]

    def test_remove(self, env):
        assert gw.remove_from_watchlist("bbb") == {"symbols": ["AAA"]}

    def test_remove_strips_exchange_suffix_and_clears_the_universe_cache(self, env):
        # FIXED: remove accepts ".NS"/".BO" like add/set, and drops the cached scan universe so a
        # removed symbol stops being scanned immediately.
        assert gw.remove_from_watchlist("aaa.ns") == {"symbols": ["BBB"]}
        assert env.redis.deleted == [gw.SCAN_UNIVERSE_KEY]
        assert gw.remove_from_watchlist(" bbb.bo ") == {"symbols": []}

    def test_remove_missing_still_writes(self, env):
        gw.remove_from_watchlist("nope")
        assert env.saved == [["AAA", "BBB"]]

    def test_over_http(self, env, tc):
        assert tc.get("/watchlist").json() == {"symbols": ["AAA", "BBB"]}
        assert tc.post("/watchlist/add", json={"symbols": ["q"]}).json()["added"] == ["Q"]
        assert tc.delete("/watchlist/AAA").json()["symbols"] == ["BBB", "Q"]
        assert tc.post("/watchlist", json={"symbols": "notalist"}).status_code == 422


class TestEventsAndSearched:
    def test_events_proxy_success(self, monkeypatch):
        seen = []
        monkeypatch.setattr(gw.httpx, "get", lambda url, **kw: (seen.append((url, kw)), FakeResp(200, {"upcoming": []}))[1])
        assert gw.get_symbol_events("tcs") == {"upcoming": []}
        assert seen[0][0] == f"{gw.EVENT_URL}/events/TCS/categorized" and seen[0][1]["timeout"] == 30

    def test_events_error_status_is_502(self, monkeypatch, logs):
        monkeypatch.setattr(gw.httpx, "get", lambda url, **kw: FakeResp(500))
        with pytest.raises(HTTPException) as e:
            gw.get_symbol_events("tcs")
        assert e.value.status_code == 502 and "event-tracker-service unreachable" in e.value.detail
        assert any("categorized events for tcs" in m for m in logs["warning"])

    def test_events_connection_error_is_502(self, monkeypatch, logs):
        def boom(url, **kw):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(gw.httpx, "get", boom)
        with pytest.raises(HTTPException) as e:
            gw.get_symbol_events("x")
        assert e.value.status_code == 502 and "refused" in e.value.detail

    def test_events_strips_exchange_suffix(self, monkeypatch):
        # FIXED: /events/TCS.NS is forwarded as TCS, like /quote and /market/history.
        seen = []
        monkeypatch.setattr(gw.httpx, "get", lambda url, **kw: (seen.append(url), FakeResp(200, {}))[1])
        gw.get_symbol_events("tcs.ns")
        assert seen[0].endswith("/events/TCS/categorized")

    def test_searched(self, monkeypatch):
        monkeypatch.setattr(gw, "_load_searched", lambda: ["A", "B"])
        assert gw.get_searched_symbols() == {"symbols": ["A", "B"]}
