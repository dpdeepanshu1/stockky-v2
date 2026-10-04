"""tests/test_main_ops_alerts_hardreset.py — coverage for api-gateway/main.py, slice 15 (lines 9055-9459)

Pass 73. The operator-facing tail that follows the startup hooks:

* `/ops/circuit-reset`, `/ops/circuit-status` — breaker reset / inspection and their error envelopes;
* `/ops/overnight/{status,config,run}` — the thin proxy to notification-scheduler-service (query-param
  assembly for POST config, `{"ok": False}` envelope on any failure);
* `/ops/keepalive` — shallow vs `deep=true` sequential soft-pings (6-service cap, blank URLs skipped,
  200 -> True, anything else -> False, back-off sleep only after an exception);
* `/ops/power-off`, `/ops/resume-activity`, `/ops/activity` — training-stop URL walk, phase list, gate reset;
* `/api/quotes/bulk-cache` + `/data-feed/bulk-cache`;
* price alerts — list / add (body validation) / delete / evaluate (notify loop, per-alert isolation);
* `/data-feed/hard-reset` — stop-feed -> kv wipe -> cache clear -> ghost-key purge -> job/meta reset, with
  every best-effort step failing independently.

Everything downstream is faked: the shared async httpx client, `httpx.AsyncClient` / `httpx.post` inside main,
the breaker registry, data_feed's alert + bulk-cache helpers, `kv_cache.hard_reset_stockky_kv`, the Redis /
kv handles and the feed store. Nothing touches the network or a database. Findings are pinned as current
behaviour and marked ``NOT FIXED``; the five found here that were safe to fix (upstream error status on the
overnight proxy, non-object / NaN / Infinity price-alert bodies, the unguarded evaluate route, a status-less wipe
result) are fixed in main.py and their tests pin the fixed behaviour.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_ops_alerts_hardreset.py -v
"""
from __future__ import annotations

import asyncio
import os
import types
from datetime import datetime

import httpx
import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)

import circuit_breaker
import data_feed
import kv_cache
from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient


def _run(coro):
    return asyncio.run(coro)


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status=200, data=None, json_raises=False):
        self.status_code = status
        self._data = {} if data is None else data
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("bad json")
        return self._data


class FakeClient:
    """Scripted async client. `routes` maps a URL substring -> FakeResp | Exception."""

    def __init__(self):
        self.routes = {}
        self.calls = []
        self.default = None

    def _resolve(self, method, url, kw):
        self.calls.append((method, url, kw))
        for frag, out in self.routes.items():
            if frag in url:
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


class FakeAsyncCtx:
    """Stands in for `httpx.AsyncClient(...)` used as `async with`."""

    script = None            # FakeResp | Exception for every request
    inits = []
    calls = []

    def __init__(self, *a, **kw):
        type(self).inits.append(kw)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kw):
        return self._go("GET", url, kw)

    async def post(self, url, **kw):
        return self._go("POST", url, kw)

    def _go(self, method, url, kw):
        type(self).calls.append((method, url, kw))
        out = type(self).script
        if isinstance(out, Exception):
            raise out
        return out


@pytest.fixture
def proxy(monkeypatch):
    """Replace `httpx` inside main so `httpx.AsyncClient(...)` is scripted."""
    FakeAsyncCtx.script = FakeResp(200, {"ok": True})
    FakeAsyncCtx.inits = []
    FakeAsyncCtx.calls = []
    monkeypatch.setattr(gw, "httpx", types.SimpleNamespace(AsyncClient=FakeAsyncCtx, post=None))
    monkeypatch.setattr(gw, "SCHEDULER_URL", "http://sched/scheduler")
    return FakeAsyncCtx


@pytest.fixture
def client(monkeypatch):
    c = FakeClient()
    monkeypatch.setattr(gw, "_get_http_client", lambda: c)
    return c


@pytest.fixture
def tc():
    return TestClient(gw.app, raise_server_exceptions=False)


@pytest.fixture
def logs(monkeypatch):
    rec = {"warning": [], "debug": [], "exception": []}

    class L:
        def warning(self, msg, *a, **k):
            rec["warning"].append(msg % a if a else msg)

        def debug(self, msg, *a, **k):
            rec["debug"].append(msg % a if a else msg)

        def exception(self, msg, *a, **k):
            rec["exception"].append(msg % a if a else msg)

        def info(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

    monkeypatch.setattr(gw, "logger", L())
    return rec


@pytest.fixture
def sleeps(monkeypatch):
    rec = []

    async def _s(d, *a, **k):
        rec.append(d)

    monkeypatch.setattr(gw.asyncio, "sleep", _s)
    return rec


# ═════════════════════════════════════════════════════════════════════════════
# /ops/circuit-reset, /ops/circuit-status
# ═════════════════════════════════════════════════════════════════════════════

class TestCircuitOps:
    def test_reset_success(self, monkeypatch):
        monkeypatch.setattr(circuit_breaker, "reset_all_breakers", lambda: ["a", "b"])
        monkeypatch.setattr(circuit_breaker, "all_snapshots", lambda: {"a": {"state": "closed"}})
        out = _run(gw.ops_circuit_reset())
        assert out == {
            "ok": True, "reset": ["a", "b"], "count": 2,
            "snapshots": {"a": {"state": "closed"}},
            "message": "Reset 2 circuit breaker(s). Ready for scan.",
        }

    def test_reset_with_no_breakers(self, monkeypatch):
        monkeypatch.setattr(circuit_breaker, "reset_all_breakers", lambda: [])
        monkeypatch.setattr(circuit_breaker, "all_snapshots", lambda: {})
        out = _run(gw.ops_circuit_reset())
        assert out["ok"] is True and out["count"] == 0
        assert out["message"] == "Reset 0 circuit breaker(s). Ready for scan."

    def test_reset_failure_is_enveloped_truncated_and_logged(self, monkeypatch, logs):
        def boom():
            raise RuntimeError("x" * 500)

        monkeypatch.setattr(circuit_breaker, "reset_all_breakers", boom)
        out = _run(gw.ops_circuit_reset())
        assert out["ok"] is False and out["error"] == "x" * 200
        assert logs["warning"] and logs["warning"][0].startswith("circuit-reset: ")

    def test_snapshot_failure_after_reset_also_enveloped(self, monkeypatch, logs):
        monkeypatch.setattr(circuit_breaker, "reset_all_breakers", lambda: ["a"])

        def boom():
            raise ValueError("snap fail")

        monkeypatch.setattr(circuit_breaker, "all_snapshots", boom)
        assert _run(gw.ops_circuit_reset()) == {"ok": False, "error": "snap fail"}

    def test_reset_is_routed_for_get_and_post(self, monkeypatch, tc):
        monkeypatch.setattr(circuit_breaker, "reset_all_breakers", lambda: ["a"])
        monkeypatch.setattr(circuit_breaker, "all_snapshots", lambda: {})
        for method in ("get", "post"):
            r = getattr(tc, method)("/ops/circuit-reset")
            assert r.status_code == 200 and r.json()["count"] == 1

    def test_status_success(self, monkeypatch, tc):
        monkeypatch.setattr(circuit_breaker, "all_snapshots", lambda: {"md": {"state": "open"}})
        r = tc.get("/ops/circuit-status")
        assert r.json() == {"ok": True, "breakers": {"md": {"state": "open"}}}

    def test_status_failure_truncated(self, monkeypatch):
        def boom():
            raise RuntimeError("y" * 300)

        monkeypatch.setattr(circuit_breaker, "all_snapshots", boom)
        assert _run(gw.ops_circuit_status()) == {"ok": False, "error": "y" * 200}


# ═════════════════════════════════════════════════════════════════════════════
# /ops/overnight/* proxy
# ═════════════════════════════════════════════════════════════════════════════

class TestOvernightProxy:
    def test_status_proxies_json(self, proxy):
        proxy.script = FakeResp(200, {"ok": True, "phase": "idle"})
        assert _run(gw.ops_overnight_status()) == {"ok": True, "phase": "idle"}
        assert proxy.calls == [("GET", "http://sched/scheduler/overnight/status", {})]
        assert proxy.inits == [{"timeout": 15.0}]

    def test_status_error_envelope(self, proxy):
        proxy.script = httpx.ConnectError("z" * 400)
        assert _run(gw.ops_overnight_status()) == {"ok": False, "error": "z" * 200}

    def test_status_non_json_body_is_enveloped(self, proxy):
        proxy.script = FakeResp(200, json_raises=True)
        assert _run(gw.ops_overnight_status()) == {"ok": False, "error": "bad json"}

    def test_status_surfaces_an_upstream_error_status(self, proxy):
        """FIXED: the upstream status code was ignored, so a 500 body was relayed as a plain 200."""
        proxy.script = FakeResp(500, {"detail": "scheduler down"})
        assert _run(gw.ops_overnight_status()) == {
            "ok": False, "error": "scheduler returned HTTP 500", "detail": {"detail": "scheduler down"}}

    def test_every_overnight_route_surfaces_an_upstream_error_status(self, proxy):
        proxy.script = FakeResp(503, {"x": 1})
        for call in (gw.ops_overnight_status, gw.ops_overnight_get_config,
                     gw.ops_overnight_set_config, gw.ops_overnight_run):
            out = _run(call())
            assert out == {"ok": False, "error": "scheduler returned HTTP 503", "detail": {"x": 1}}

    def test_a_2xx_and_3xx_body_is_still_relayed_unchanged(self, proxy):
        for code in (200, 204, 302):
            proxy.script = FakeResp(code, {"enabled": True})
            assert _run(gw.ops_overnight_status()) == {"enabled": True}

    def test_get_config_proxies(self, proxy):
        proxy.script = FakeResp(200, {"enabled": True})
        assert _run(gw.ops_overnight_get_config()) == {"enabled": True}
        assert proxy.calls[0][:2] == ("GET", "http://sched/scheduler/overnight/config")

    def test_get_config_error(self, proxy):
        proxy.script = RuntimeError("nope")
        assert _run(gw.ops_overnight_get_config()) == {"ok": False, "error": "nope"}

    def test_set_config_without_params_sends_empty_params(self, proxy):
        proxy.script = FakeResp(200, {"ok": True})
        _run(gw.ops_overnight_set_config())
        assert proxy.calls == [("POST", "http://sched/scheduler/overnight/config", {"params": {}})]

    def test_set_config_all_params(self, proxy):
        _run(gw.ops_overnight_set_config(enabled=True, datafeed_time="22:00",
                                         premarket_time="08:30", rest_between_steps_sec=90))
        assert proxy.calls[0][2]["params"] == {
            "enabled": True, "datafeed_time": "22:00",
            "premarket_time": "08:30", "rest_between_steps_sec": 90,
        }

    def test_set_config_false_and_zero_are_forwarded(self, proxy):
        """`is not None` checks — falsy-but-set values must not be dropped."""
        _run(gw.ops_overnight_set_config(enabled=False, rest_between_steps_sec=0))
        assert proxy.calls[0][2]["params"] == {"enabled": False, "rest_between_steps_sec": 0}

    def test_set_config_empty_string_times_are_forwarded(self, proxy):
        _run(gw.ops_overnight_set_config(datafeed_time="", premarket_time=""))
        assert proxy.calls[0][2]["params"] == {"datafeed_time": "", "premarket_time": ""}

    def test_set_config_error(self, proxy):
        proxy.script = httpx.ReadTimeout("slow")
        assert _run(gw.ops_overnight_set_config(enabled=True)) == {"ok": False, "error": "slow"}

    def test_set_config_through_the_router_parses_query_params(self, proxy, tc):
        r = tc.post("/ops/overnight/config?enabled=false&rest_between_steps_sec=5")
        assert r.status_code == 200
        assert proxy.calls[0][2]["params"] == {"enabled": False, "rest_between_steps_sec": 5}

    def test_run_defaults_to_datafeed_phase(self, proxy):
        proxy.script = FakeResp(200, {"started": True})
        assert _run(gw.ops_overnight_run()) == {"started": True}
        assert proxy.calls == [("POST", "http://sched/scheduler/overnight/run",
                                {"params": {"phase": "datafeed"}})]

    def test_run_custom_phase_and_error(self, proxy):
        _run(gw.ops_overnight_run(phase="premarket"))
        assert proxy.calls[0][2]["params"] == {"phase": "premarket"}
        proxy.script = httpx.ConnectError("down")
        assert _run(gw.ops_overnight_run(phase="premarket")) == {"ok": False, "error": "down"}


# ═════════════════════════════════════════════════════════════════════════════
# /ops/keepalive
# ═════════════════════════════════════════════════════════════════════════════

class TestKeepalive:
    def test_shallow_makes_no_upstream_calls(self, client):
        out = _run(gw.ops_keepalive(deep=False))
        assert out["ok"] is True and out["gateway"] is True and out["services"] == {}
        assert client.calls == []
        datetime.fromisoformat(out["ts"])          # valid ISO timestamp

    def test_deep_pings_each_service_sequentially(self, monkeypatch, client, sleeps):
        monkeypatch.setattr(gw, "SYSTEM_SERVICES", {
            "ok-svc": {"url": "http://a/"},
            "sick-svc": {"url": "http://b"},
            "dead-svc": {"url": "http://c"},
        })
        client.routes = {"http://a": FakeResp(200), "http://b": FakeResp(503),
                         "http://c": httpx.ConnectError("refused")}
        out = _run(gw.ops_keepalive(deep=True))
        assert out["services"] == {"ok-svc": True, "sick-svc": False, "dead-svc": False}
        assert out["ok"] is True
        assert [c[1] for c in client.calls] == ["http://a/health", "http://b/health", "http://c/health"]
        for _, _, kw in client.calls:
            assert kw == {"params": {"warm": "true"}, "timeout": 4.0}
        assert sleeps == [0.15]                     # only the exception path backs off

    def test_deep_skips_blank_and_missing_urls(self, monkeypatch, client, sleeps):
        monkeypatch.setattr(gw, "SYSTEM_SERVICES", {
            "blank": {"url": ""}, "none": {"url": None}, "absent": {}, "real": {"url": "http://r"},
        })
        client.default = FakeResp(200)
        out = _run(gw.ops_keepalive(deep=True))
        assert out["services"] == {"real": True}
        assert len(client.calls) == 1

    def test_deep_is_capped_at_six_services(self, monkeypatch, client, sleeps):
        monkeypatch.setattr(gw, "SYSTEM_SERVICES",
                            {f"s{i}": {"url": f"http://h{i}"} for i in range(9)})
        client.default = FakeResp(200)
        out = _run(gw.ops_keepalive(deep=True))
        assert list(out["services"]) == [f"s{i}" for i in range(6)]
        assert len(client.calls) == 6

    def test_blank_urls_count_toward_the_cap(self, monkeypatch, client, sleeps):
        """The `[:6]` slice is taken before the blank-URL filter."""
        svc = {f"b{i}": {"url": ""} for i in range(6)}
        svc["late"] = {"url": "http://late"}
        monkeypatch.setattr(gw, "SYSTEM_SERVICES", svc)
        client.default = FakeResp(200)
        assert _run(gw.ops_keepalive(deep=True))["services"] == {}
        assert client.calls == []

    def test_non_200_success_codes_count_as_down(self, monkeypatch, client, sleeps):
        monkeypatch.setattr(gw, "SYSTEM_SERVICES", {"x": {"url": "http://x"}})
        client.default = FakeResp(204)
        assert _run(gw.ops_keepalive(deep=True))["services"] == {"x": False}

    def test_routed_for_get_and_post_with_deep_flag(self, monkeypatch, client, sleeps, tc):
        monkeypatch.setattr(gw, "SYSTEM_SERVICES", {"x": {"url": "http://x"}})
        client.default = FakeResp(200)
        for method in ("get", "post"):
            assert getattr(tc, method)("/ops/keepalive").json()["services"] == {}
            assert getattr(tc, method)("/ops/keepalive?deep=true").json()["services"] == {"x": True}


# ═════════════════════════════════════════════════════════════════════════════
# /ops/power-off, /ops/resume-activity, /ops/activity
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def poweroff(monkeypatch, client):
    reasons = []

    def commit(reason="shutdown"):
        reasons.append(reason)
        return [{"phase": "activity_gate", "ok": True, "detail": "paused (power_off)"}]

    monkeypatch.setattr(gw, "_graceful_shutdown_commit", commit)
    monkeypatch.setattr(gw, "TRAINING_URL", "http://train/training/")
    monkeypatch.setattr(gw, "DECISION_URL", "http://dec/decision/")
    return types.SimpleNamespace(client=client, reasons=reasons)


class TestPowerOff:
    def test_first_training_url_succeeds(self, poweroff):
        poweroff.client.default = FakeResp(200)
        out = _run(gw.ops_power_off(BackgroundTasks()))
        assert poweroff.reasons == ["power_off"]
        assert [c[1] for c in poweroff.client.calls] == ["http://train/training/api/train/stop"]
        assert poweroff.client.calls[0][2] == {"timeout": 8}
        assert out["phases"][1] == {"phase": "training", "ok": True, "detail": "stop signalled"}

    def test_walks_urls_until_a_non_5xx_reply(self, poweroff):
        poweroff.client.routes = {
            "/api/train/stop": httpx.ConnectError("refused"),
            "/training/stop": FakeResp(404),            # < 500 counts as signalled
        }
        out = _run(gw.ops_power_off(BackgroundTasks()))
        assert [c[1] for c in poweroff.client.calls] == [
            "http://train/training/api/train/stop", "http://train/training/training/stop"]
        assert out["phases"][1]["detail"] == "stop signalled"

    def test_all_urls_failing_is_best_effort_but_still_ok(self, poweroff):
        poweroff.client.routes = {
            "/api/train/stop": FakeResp(500),
            "/training/stop": httpx.ReadTimeout("slow"),
            "/training/clear-lock": FakeResp(503),
        }
        out = _run(gw.ops_power_off(BackgroundTasks()))
        assert [c[1] for c in poweroff.client.calls][-1] == "http://dec/decision/training/clear-lock"
        assert len(poweroff.client.calls) == 3
        assert out["phases"][1] == {"phase": "training", "ok": True, "detail": "best-effort"}

    def test_client_construction_failure_is_recorded_not_raised(self, monkeypatch, poweroff):
        def boom():
            raise RuntimeError("c" * 300)

        monkeypatch.setattr(gw, "_get_http_client", boom)
        out = _run(gw.ops_power_off(BackgroundTasks()))
        assert out["phases"][1] == {"phase": "training", "ok": False, "detail": "c" * 120}
        assert out["ok"] is True

    def test_response_shape_and_phase_order(self, poweroff):
        poweroff.client.default = FakeResp(200)
        out = _run(gw.ops_power_off(BackgroundTasks()))
        assert [p["phase"] for p in out["phases"]] == ["activity_gate", "training", "ready"]
        assert out["phases"][-1]["ok"] is True
        assert out["ok"] is True and out["activity_paused"] is True
        assert out["message"].startswith("Power Off complete")
        assert "hint" in out

    def test_routed_as_post_only(self, poweroff, tc):
        poweroff.client.default = FakeResp(200)
        assert tc.post("/ops/power-off").status_code == 200
        assert tc.get("/ops/power-off").status_code == 405


class TestResumeAndActivity:
    def test_resume_clears_gate_and_global_cancel_flag_only(self, monkeypatch):
        seen = []
        monkeypatch.setattr(gw, "set_activity_paused", lambda p: seen.append(p))
        flags = {"__ALL__", "scan-123"}
        monkeypatch.setattr(gw, "_SCAN_CANCEL_FLAGS", flags)
        out = gw.ops_resume_activity()
        assert out == {"ok": True, "activity_paused": False, "message": "Activity resumed"}
        assert seen == [False]
        assert flags == {"scan-123"}                 # per-task flags survive

    def test_resume_without_flag_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(gw, "set_activity_paused", lambda p: None)
        monkeypatch.setattr(gw, "_SCAN_CANCEL_FLAGS", set())
        assert gw.ops_resume_activity()["ok"] is True

    def test_resume_survives_a_broken_flag_set(self, monkeypatch):
        class Broken:
            def discard(self, _):
                raise RuntimeError("frozen")

        monkeypatch.setattr(gw, "set_activity_paused", lambda p: None)
        monkeypatch.setattr(gw, "_SCAN_CANCEL_FLAGS", Broken())
        assert gw.ops_resume_activity()["activity_paused"] is False

    def test_activity_status(self, monkeypatch):
        monkeypatch.setattr(gw, "_ACTIVITY_PAUSED", True)
        monkeypatch.setattr(gw, "_QUOTE_LOOP_ENABLED", False)
        monkeypatch.setattr(gw, "_SCAN_CANCEL_FLAGS", {"a", "b", "__ALL__"})
        assert gw.ops_activity_status() == {
            "ok": True, "activity_paused": True, "quote_loop_enabled": False,
            "scan_cancel_flags": 3,
        }

    def test_activity_status_running_state(self, monkeypatch, tc):
        monkeypatch.setattr(gw, "_ACTIVITY_PAUSED", False)
        monkeypatch.setattr(gw, "_QUOTE_LOOP_ENABLED", True)
        monkeypatch.setattr(gw, "_SCAN_CANCEL_FLAGS", set())
        assert tc.get("/ops/activity").json() == {
            "ok": True, "activity_paused": False, "quote_loop_enabled": True,
            "scan_cancel_flags": 0,
        }


# ═════════════════════════════════════════════════════════════════════════════
# bulk quote cache
# ═════════════════════════════════════════════════════════════════════════════

class TestBulkCache:
    def _patch(self, monkeypatch, cache, age=12.5, refresh=True):
        monkeypatch.setattr(data_feed, "get_bulk_quote_cache", lambda: cache)
        monkeypatch.setattr(data_feed, "bulk_cache_age_sec", lambda: age)
        monkeypatch.setattr(data_feed, "should_refresh_bulk_cache", lambda: refresh)

    def test_populated_cache(self, monkeypatch):
        self._patch(monkeypatch, {"_meta": {"src": "yf"}, "quotes": {"TCS": {"price": 1}, "INFY": {}}})
        out = _run(gw.api_bulk_quote_cache())
        assert out == {"ok": True, "age_sec": 12.5, "should_refresh": True,
                       "meta": {"src": "yf"}, "count": 2,
                       "quotes": {"TCS": {"price": 1}, "INFY": {}}}

    def test_empty_cache_object(self, monkeypatch):
        self._patch(monkeypatch, {}, age=None, refresh=False)
        out = _run(gw.api_bulk_quote_cache())
        assert out == {"ok": True, "age_sec": None, "should_refresh": False,
                       "meta": None, "count": 0, "quotes": {}}

    def test_none_cache_and_null_quotes(self, monkeypatch):
        self._patch(monkeypatch, None)
        assert _run(gw.api_bulk_quote_cache())["count"] == 0
        self._patch(monkeypatch, {"_meta": {"m": 1}, "quotes": None})
        out = _run(gw.api_bulk_quote_cache())
        assert out["meta"] == {"m": 1} and out["quotes"] == {} and out["count"] == 0

    def test_both_paths_are_routed(self, monkeypatch, tc):
        self._patch(monkeypatch, {"quotes": {"A": {}}})
        for path in ("/api/quotes/bulk-cache", "/data-feed/bulk-cache"):
            r = tc.get(path)
            assert r.status_code == 200 and r.json()["count"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# price alerts
# ═════════════════════════════════════════════════════════════════════════════

class TestListAndDeleteAlerts:
    def test_list(self, monkeypatch, tc):
        monkeypatch.setattr(data_feed, "list_price_alerts", lambda: [{"id": "1"}, {"id": "2"}])
        for path in ("/api/price-alerts", "/price-alerts"):
            assert tc.get(path).json() == {"ok": True, "alerts": [{"id": "1"}, {"id": "2"}], "count": 2}

    def test_list_empty(self, monkeypatch):
        monkeypatch.setattr(data_feed, "list_price_alerts", lambda: [])
        assert _run(gw.api_list_price_alerts()) == {"ok": True, "alerts": [], "count": 0}

    def test_delete_found(self, monkeypatch, tc):
        seen = []
        monkeypatch.setattr(data_feed, "delete_price_alert", lambda i: seen.append(i) or True)
        for path in ("/api/price-alerts/TCS-above-1", "/price-alerts/TCS-above-1"):
            assert tc.delete(path).json() == {"ok": True, "deleted": "TCS-above-1"}
        assert seen == ["TCS-above-1", "TCS-above-1"]

    def test_delete_missing_is_404(self, monkeypatch, tc):
        monkeypatch.setattr(data_feed, "delete_price_alert", lambda i: False)
        r = tc.delete("/api/price-alerts/nope")
        assert r.status_code == 404 and r.json()["detail"] == "alert not found"

    def test_delete_direct_raises_http_exception(self, monkeypatch):
        monkeypatch.setattr(data_feed, "delete_price_alert", lambda i: False)
        with pytest.raises(HTTPException) as ei:
            _run(gw.api_delete_price_alert("zzz"))
        assert ei.value.status_code == 404


class TestAddAlert:
    @pytest.fixture
    def added(self, monkeypatch):
        calls = []

        def add(sym, target, direction="above", note=""):
            calls.append({"sym": sym, "target": target, "direction": direction, "note": note})
            return {"id": "new", "symbol": sym}

        monkeypatch.setattr(data_feed, "add_price_alert", add)
        return calls

    def test_happy_path_defaults(self, added, tc):
        r = tc.post("/api/price-alerts", json={"symbol": " TCS ", "target_price": 4000})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "alert": {"id": "new", "symbol": "TCS"}}
        assert added == [{"sym": "TCS", "target": 4000.0, "direction": "above", "note": ""}]

    def test_alias_target_direction_lowercased_and_note(self, added, tc):
        r = tc.post("/price-alerts", json={"symbol": "INFY", "target": "1500.5",
                                           "direction": "BELOW", "note": "dip"})
        assert r.status_code == 200
        assert added == [{"sym": "INFY", "target": 1500.5, "direction": "below", "note": "dip"}]

    def test_target_price_zero_falls_back_to_alias(self, added, tc):
        tc.post("/price-alerts", json={"symbol": "A", "target_price": 0, "target": 7})
        assert added[0]["target"] == 7.0

    def test_direction_is_not_validated_by_the_gateway(self, added, tc):
        """data_feed.add_price_alert normalises it; the route passes any string through."""
        tc.post("/price-alerts", json={"symbol": "A", "target_price": 1, "direction": "Sideways"})
        assert added[0]["direction"] == "sideways"

    @pytest.mark.parametrize("body", [
        {}, {"symbol": "TCS"}, {"target_price": 10}, {"symbol": "   ", "target_price": 10},
        {"symbol": "TCS", "target_price": 0}, {"symbol": "TCS", "target_price": -5},
        {"symbol": "TCS", "target_price": "abc"}, {"symbol": "TCS", "target_price": [1]},
    ])
    def test_invalid_bodies_are_400(self, added, tc, body):
        r = tc.post("/api/price-alerts", json=body)
        assert r.status_code == 400
        assert r.json()["detail"] == "symbol and target_price required"
        assert added == []

    def test_unparseable_body_is_treated_as_empty(self, added, tc):
        r = tc.post("/api/price-alerts", content=b"not json",
                    headers={"content-type": "application/json"})
        assert r.status_code == 400 and added == []

    def test_json_null_and_empty_list_are_treated_as_empty(self, added, tc):
        for raw in (b"null", b"[]"):
            r = tc.post("/api/price-alerts", content=raw, headers={"content-type": "application/json"})
            assert r.status_code == 400
        assert added == []

    @pytest.mark.parametrize("body", [b"[1]", b'"text"', b"42", b"true"])
    def test_non_object_body_is_a_400(self, added, tc, body):
        """FIXED: only falsy bodies were coerced to {}; a truthy non-object had no `.get` -> bare 500."""
        r = tc.post("/api/price-alerts", content=body, headers={"content-type": "application/json"})
        assert r.status_code == 400 and "JSON object" in r.json()["detail"] and added == []

    @pytest.mark.parametrize("raw", [b"NaN", b"Infinity", b"-Infinity"])
    def test_non_finite_target_is_a_400(self, added, tc, raw):
        """FIXED: `float('nan') <= 0` is False, so a NaN/Infinity target (json.loads accepts both) passed
        validation and reached add_price_alert."""
        r = tc.post("/api/price-alerts", content=b'{"symbol": "TCS", "target_price": ' + raw + b"}",
                    headers={"content-type": "application/json"})
        assert r.status_code == 400 and "target_price" in r.json()["detail"] and added == []

    def test_non_finite_value_in_the_target_alias_is_also_a_400(self, added, tc):
        r = tc.post("/api/price-alerts", content=b'{"symbol": "TCS", "target": NaN}',
                    headers={"content-type": "application/json"})
        assert r.status_code == 400 and added == []

    def test_a_normal_finite_target_still_goes_through(self, added, tc):
        r = tc.post("/api/price-alerts", json={"symbol": "TCS", "target_price": 4100.5})
        assert r.status_code == 200 and added[0]["target"] == 4100.5

    def test_non_string_symbol_and_note_are_stringified(self, added, tc):
        tc.post("/api/price-alerts", json={"symbol": 500325, "target_price": 1, "note": 42})
        assert added[0]["sym"] == "500325" and added[0]["note"] == "42"


class TestEvaluateAlerts:
    @pytest.fixture
    def env(self, monkeypatch):
        e = types.SimpleNamespace(triggered=[], wakes=0, posts=[], post_script=None, wake_raises=False)
        monkeypatch.setattr(data_feed, "evaluate_price_alerts", lambda: e.triggered)

        def wake():
            e.wakes += 1
            if e.wake_raises:
                raise RuntimeError("wake failed")
            return True

        def post(url, **kw):
            e.posts.append((url, kw))
            out = e.post_script
            if callable(out):
                out = out(len(e.posts))
            if isinstance(out, Exception):
                raise out
            return out if out is not None else FakeResp(200)

        monkeypatch.setattr(gw, "_wake_notification_service", wake)
        monkeypatch.setattr(gw, "NOTIFICATION_URL", "http://notif")
        monkeypatch.setattr(gw, "httpx", types.SimpleNamespace(post=post))
        return e

    def test_nothing_triggered(self, env):
        assert _run(gw.api_evaluate_price_alerts()) == {
            "ok": True, "triggered": [], "triggered_count": 0, "notified": 0}
        assert env.wakes == 0 and env.posts == []

    def test_triggered_alert_is_notified_with_note(self, env):
        env.triggered = [{"symbol": "TCS", "current_price": 4010, "direction": "above",
                          "target_price": 4000, "note": "breakout"}]
        out = _run(gw.api_evaluate_price_alerts())
        assert out["triggered_count"] == 1 and out["notified"] == 1 and out["triggered"] == env.triggered
        assert env.wakes == 1
        url, kw = env.posts[0]
        assert url == "http://notif/notify" and kw["timeout"] == 12
        assert kw["json"] == {
            "title": "Price Alert · TCS",
            "message": "Price alert: TCS is ₹4010 (above ₹4000) — breakout",
            "channel": "all",
        }

    def test_message_without_note_has_no_suffix(self, env):
        env.triggered = [{"symbol": "INFY", "current_price": 1, "direction": "below", "target_price": 2}]
        _run(gw.api_evaluate_price_alerts())
        assert env.posts[0][1]["json"]["message"] == "Price alert: INFY is ₹1 (below ₹2)"

    def test_missing_fields_render_as_none(self, env):
        env.triggered = [{}]
        out = _run(gw.api_evaluate_price_alerts())
        assert out["notified"] == 1
        assert env.posts[0][1]["json"]["title"] == "Price Alert · None"

    def test_only_exactly_200_is_counted(self, env):
        env.triggered = [{"symbol": s} for s in "ABCD"]
        codes = {1: 500, 2: 404, 3: 201, 4: 200}
        env.post_script = lambda n: FakeResp(codes[n])
        out = _run(gw.api_evaluate_price_alerts())
        assert out["triggered_count"] == 4 and out["notified"] == 1

    def test_post_failure_does_not_stop_later_alerts(self, env, logs):
        env.triggered = [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}]
        env.post_script = lambda n: httpx.ConnectError("down") if n == 2 else FakeResp(200)
        out = _run(gw.api_evaluate_price_alerts())
        assert out["notified"] == 2 and len(env.posts) == 3
        assert any(m.startswith("price alert notify: ") for m in logs["debug"])

    def test_wake_failure_skips_the_post_for_that_alert(self, env, logs):
        env.triggered = [{"symbol": "A"}, {"symbol": "B"}]
        env.wake_raises = True
        out = _run(gw.api_evaluate_price_alerts())
        assert out["notified"] == 0 and env.posts == [] and env.wakes == 2
        assert out["triggered_count"] == 2

    def test_evaluation_failure_is_an_error_envelope(self, monkeypatch, tc):
        """FIXED: unlike its siblings this route had no try/except around evaluate_price_alerts, so a kv
        failure was a bare 500 / an exception out of the handler."""
        def boom():
            raise RuntimeError("kv down")

        monkeypatch.setattr(data_feed, "evaluate_price_alerts", boom)
        expected = {"ok": False, "error": "kv down", "triggered": [], "triggered_count": 0, "notified": 0}
        r = tc.post("/api/price-alerts/evaluate")
        assert r.status_code == 200 and r.json() == expected
        assert _run(gw.api_evaluate_price_alerts()) == expected

    def test_evaluation_failure_error_text_is_truncated_to_200(self, monkeypatch):
        def boom():
            raise RuntimeError("k" * 500)

        monkeypatch.setattr(data_feed, "evaluate_price_alerts", boom)
        assert _run(gw.api_evaluate_price_alerts())["error"] == "k" * 200

    def test_both_paths_are_routed(self, env, tc):
        env.triggered = [{"symbol": "A"}]
        for path in ("/api/price-alerts/evaluate", "/price-alerts/evaluate"):
            assert tc.post(path).json()["notified"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# /data-feed/hard-reset
# ═════════════════════════════════════════════════════════════════════════════

class FakeStore:
    def __init__(self, events):
        self.events = events
        self.job_kw = None
        self.meta_kw = None
        self.raise_on_job = False

    def set_job(self, **kw):
        if self.raise_on_job:
            raise RuntimeError("store down")
        self.job_kw = kw
        self.events.append("set_job")

    def set_meta(self, **kw):
        self.meta_kw = kw
        self.events.append("set_meta")


@pytest.fixture
def hr(monkeypatch):
    """Everything hard_reset_database touches, recorded in one ordered event list."""
    e = types.SimpleNamespace(events=[], preserve=[], kv_deleted=[], redis_deleted=[],
                              result={"status": "ok"}, kv_raises=False, redis_raises=False,
                              raises={})
    e.store = FakeStore(e.events)

    def step(name):
        def _f():
            e.events.append(name)
            if name in e.raises:
                raise RuntimeError(f"{name} failed")
        return _f

    def wipe(preserve_days=7):
        e.events.append("wipe")
        e.preserve.append(preserve_days)
        if isinstance(e.result, Exception):
            raise e.result
        return e.result

    monkeypatch.setattr(kv_cache, "hard_reset_stockky_kv", wipe)
    monkeypatch.setattr(data_feed, "request_data_feed_stop", step("stop"))
    monkeypatch.setattr(data_feed, "clear_local_data_feed_caches", step("clear_local"))
    monkeypatch.setattr(data_feed, "clear_data_feed_stop", step("clear_stop"))

    def kv_delete(k):
        if e.kv_raises:
            raise RuntimeError("kv down")
        e.kv_deleted.append(k)

    def redis_delete(k):
        if e.redis_raises:
            raise RuntimeError("redis down")
        e.redis_deleted.append(k)

    monkeypatch.setattr(gw, "_kv_cache", types.SimpleNamespace(kv_delete=kv_delete))
    monkeypatch.setattr(gw, "_redis", types.SimpleNamespace(delete=redis_delete))
    monkeypatch.setattr(gw, "_feed_store", lambda: e.store)
    return e


GHOSTS = [
    "stockky:scan_universe", "stockky:last_full_scan", "stockky:known_symbols",
    "stockky:data_feed:index", "stockky:data_feed:meta", "stockky:data_feed:job",
    "stockky:hot_result", "stockky:hot_result_db", "stockky:hot_job",
]


class TestHardReset:
    def test_happy_path(self, hr):
        out = _run(gw.hard_reset_database())
        assert out["status"] == "ok"
        assert out["message"] == "Database wiped, locked, and memory cleared. Ready for feed."
        assert out["ghosts_cleared"] == GHOSTS
        assert hr.kv_deleted == GHOSTS and hr.redis_deleted == GHOSTS
        assert hr.preserve == [7]

    def test_ghost_list_starts_with_the_scan_universe_key(self, hr):
        assert gw.SCAN_UNIVERSE_KEY == GHOSTS[0]

    def test_steps_run_in_order(self, hr):
        _run(gw.hard_reset_database())
        assert hr.events == ["stop", "wipe", "clear_local", "clear_stop", "set_job", "set_meta"]

    def test_job_and_meta_are_reset(self, hr):
        _run(gw.hard_reset_database())
        assert hr.store.job_kw == {
            "status": "idle", "message": "Hard-reset complete — ready for fresh feed",
            "stop_requested": False, "processed": 0, "total": 0, "ok_count": 0,
        }
        assert hr.store.meta_kw == {
            "last_success_at": None, "last_count": 0,
            "last_message": "Hard-reset — memory + DB wiped", "stock_count": 0,
        }

    def test_preserve_days_is_forwarded(self, hr):
        _run(gw.hard_reset_database(preserve_days=0))
        _run(gw.hard_reset_database(preserve_days=30))
        assert hr.preserve == [0, 30]

    def test_existing_result_message_is_kept(self, hr):
        hr.result = {"status": "ok", "message": "restored 12 rows", "restored": 12}
        out = _run(gw.hard_reset_database())
        assert out["message"] == "restored 12 rows" and out["restored"] == 12

    def test_empty_message_falls_back_to_default(self, hr):
        hr.result = {"status": "ok", "message": ""}
        assert _run(gw.hard_reset_database())["message"].startswith("Database wiped")

    def test_error_status_raises_500_after_cleanup(self, hr):
        hr.result = {"status": "error", "message": "neon unreachable"}
        with pytest.raises(HTTPException) as ei:
            _run(gw.hard_reset_database())
        assert ei.value.status_code == 500 and ei.value.detail == "neon unreachable"
        # the ghost purge and job/meta reset still happened before the error was surfaced
        assert hr.kv_deleted == GHOSTS and hr.events[-2:] == ["set_job", "set_meta"]

    def test_error_status_without_message_uses_default_detail(self, hr):
        hr.result = {"status": "error"}
        with pytest.raises(HTTPException) as ei:
            _run(gw.hard_reset_database())
        assert ei.value.detail == "hard-reset failed"

    def test_wipe_exception_becomes_500_with_truncated_detail(self, hr, logs):
        hr.result = RuntimeError("w" * 500)
        with pytest.raises(HTTPException) as ei:
            _run(gw.hard_reset_database())
        assert ei.value.status_code == 500 and ei.value.detail == "w" * 240
        assert logs["exception"] and logs["exception"][0].startswith("hard_reset_database: ")
        assert hr.kv_deleted == []                    # nothing after the wipe ran
        assert "clear_local" not in hr.events

    @pytest.mark.parametrize("result", [None, "done", ["x"], 0])
    def test_non_dict_result_is_a_success_with_the_default_message(self, hr, result):
        """FIXED: a None result from the wipe (no status contract) was an AttributeError -> 500, reported
        AFTER the wipe, job reset and ghost purge had already run."""
        hr.result = result
        out = _run(gw.hard_reset_database())
        assert out["message"] == "Database wiped, locked, and memory cleared. Ready for feed."
        assert out["ghosts_cleared"] == GHOSTS
        assert "wipe" in hr.events and hr.events[-2:] == ["set_job", "set_meta"]

    @pytest.mark.parametrize("failing", ["stop", "clear_local", "clear_stop"])
    def test_each_best_effort_step_fails_independently(self, hr, failing):
        hr.raises[failing] = True
        out = _run(gw.hard_reset_database())
        assert out["status"] == "ok" and out["ghosts_cleared"] == GHOSTS
        assert hr.events[-2:] == ["set_job", "set_meta"]
        assert "wipe" in hr.events

    def test_kv_and_redis_delete_failures_are_swallowed_per_key(self, hr):
        hr.kv_raises = True
        hr.redis_raises = True
        out = _run(gw.hard_reset_database())
        assert out["ghosts_cleared"] == GHOSTS          # the list is reported regardless
        assert hr.kv_deleted == [] and hr.redis_deleted == []

    def test_kv_failure_does_not_block_redis_delete(self, hr):
        hr.kv_raises = True
        _run(gw.hard_reset_database())
        assert hr.redis_deleted == GHOSTS

    def test_missing_kv_cache_and_redis_handles_are_skipped(self, monkeypatch, hr):
        monkeypatch.setattr(gw, "_kv_cache", None)
        monkeypatch.setattr(gw, "_redis", None)
        out = _run(gw.hard_reset_database())
        assert out["status"] == "ok" and hr.kv_deleted == [] and hr.redis_deleted == []

    def test_falsy_redis_handle_is_skipped(self, monkeypatch, hr):
        monkeypatch.setattr(gw, "_redis", 0)
        _run(gw.hard_reset_database())
        assert hr.redis_deleted == []

    def test_feed_store_failure_is_logged_and_ignored(self, hr, logs):
        hr.store.raise_on_job = True
        out = _run(gw.hard_reset_database())
        assert out["status"] == "ok"
        assert any(m.startswith("hard-reset job/meta clear: ") for m in logs["debug"])
        assert hr.store.meta_kw is None                # set_meta never reached after set_job raised

    def test_all_three_paths_are_routed_with_query_param(self, hr, tc):
        for path in ("/data-feed/hard-reset", "/api/data-feed/hard-reset", "/api/feed/hard-reset"):
            r = tc.post(f"{path}?preserve_days=3")
            assert r.status_code == 200 and r.json()["ghosts_cleared"] == GHOSTS
        assert hr.preserve == [3, 3, 3]

    def test_routed_error_is_a_500_with_detail(self, hr, tc):
        hr.result = {"status": "error", "message": "locked"}
        r = tc.post("/data-feed/hard-reset")
        assert r.status_code == 500 and r.json()["detail"] == "locked"

    def test_get_is_a_405_and_never_wipes(self, hr, tc):
        """FIXED: GET used to fall into the `/data-feed/{symbol}` catch-all (symbol="hard-reset") and
        answer 200. It is now a real 405 on every prefix, and nothing is wiped."""
        for path in ("/data-feed/hard-reset", "/api/data-feed/hard-reset", "/api/feed/hard-reset"):
            r = tc.get(path)
            assert r.status_code == 405 and r.headers["allow"] == "POST"
        assert hr.events == [] and hr.kv_deleted == []
