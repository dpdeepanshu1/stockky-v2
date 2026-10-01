"""tests/test_main_ipo_premarket_routes.py — coverage for api-gateway/main.py, slice 12 (lines 7777-8231)

Pass 70. The IPO Tracker routes, the Surprise premarket routes and the Surprise Stop button:

* `/ipo/scan` — already-running short-circuit, background task vs inline run, `force` / `wipe` pass-through;
* `/ipo/status`, `/ipo/list`, `/ipo/audit`, `/ipo/stop` — happy paths and their error envelopes;
* `POST /ipo/add`, `POST /ipo/repair-batch`, `POST /ipo/purge-non-equity`;
* `POST /ipo/notify-top-picks` — message format, `top_n`, delivery result mapping;
* `GET /surprise/premarket/status`;
* `/surprise/premarket` — the schema gate (confirmed-once flag, 503 mapping), symbol parsing (body / query),
  `background` / `force` flags, the already-running reply, the background thread job (explicit vs deferred
  universe, normalisation, default-universe fallback) and the synchronous run;
* `POST /surprise/stop`.

`ipo_scanner`, `surprise_premarket` and `surprise_schema` are replaced by fake modules in `sys.modules`
(the routes import them lazily); the premarket proxy is called directly with a fake request so the
background `threading.Thread` can be captured instead of started. The sync `httpx.post` is faked. Nothing
touches the network, a database or a real thread. Findings are pinned as current behaviour and marked
``NOT FIXED``.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_ipo_premarket_routes.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
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

from fastapi.testclient import TestClient

NOTIF = "http://notif.local"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client():
    return TestClient(gw.app, raise_server_exceptions=False)


# ═════════════════════════════════════════════════════════════════════════════
# Fakes
# ═════════════════════════════════════════════════════════════════════════════

class IEnv:
    def __init__(self):
        # ipo_scanner
        self.ipo_progress = {"status": "idle"}
        self.ipo_progress_raises = None
        self.scan_calls = []
        self.scan_result = {"count": 3}
        self.listing = {"results": [], "generated_at": None}
        self.list_calls = []
        self.list_raises = None
        self.audit_result = {"ok": True, "rows": [{"symbol": "A"}]}
        self.audit_raises = None
        self.stop_calls = 0
        self.stop_raises = None
        self.add_calls = []
        self.add_result = {"resolved": True, "entry": {"symbol": "NEW"}}
        self.add_raises = None
        self.repair_calls = []
        self.repair_result = {"repaired": 2}
        self.repair_raises = None
        self.purge_result = {"deleted": 4}
        self.purge_raises = None
        # surprise_schema / surprise_premarket
        self.schema_calls = 0
        self.schema = {"ok": True, "backend": "postgres"}
        self.schema_raises = None
        self.prem_progress = {"is_running": False}
        self.prem_progress_raises = None
        self.prem_stop_calls = 0
        self.prem_stop_raises = None
        self.base_calls = []
        self.base_result = {"ok": True, "processed": 2}
        self.base_raises = None
        self.default_universe = ["DEF1", "DEF2"]
        # gateway helper
        self.universe_calls = 0
        self.universe = ["UNI1", "UNI2"]
        self.universe_raises = None


@pytest.fixture
def ienv(monkeypatch):
    env = IEnv()

    ipo = types.ModuleType("ipo_scanner")

    def get_ipo_scan_progress():
        if env.ipo_progress_raises:
            raise env.ipo_progress_raises
        return dict(env.ipo_progress)

    def run_ipo_scan(force=False, wipe=False):
        env.scan_calls.append({"force": force, "wipe": wipe})
        return dict(env.scan_result)

    def get_ipo_list(display_days=None):
        env.list_calls.append(display_days)
        if env.list_raises:
            raise env.list_raises
        return env.listing

    def get_ipo_feed_audit():
        if env.audit_raises:
            raise env.audit_raises
        return env.audit_result

    def request_ipo_stop():
        env.stop_calls += 1
        if env.stop_raises:
            raise env.stop_raises

    def add_manual_ipo_by_name(name):
        env.add_calls.append(name)
        if env.add_raises:
            raise env.add_raises
        return env.add_result

    def ipo_repair_batch(limit=15, symbol=None):
        env.repair_calls.append({"limit": limit, "symbol": symbol})
        if env.repair_raises:
            raise env.repair_raises
        return env.repair_result

    def purge_non_equity_ipos():
        if env.purge_raises:
            raise env.purge_raises
        return env.purge_result

    for fn in (get_ipo_scan_progress, run_ipo_scan, get_ipo_list, get_ipo_feed_audit, request_ipo_stop,
               add_manual_ipo_by_name, ipo_repair_batch, purge_non_equity_ipos):
        setattr(ipo, fn.__name__, fn)

    prem = types.ModuleType("surprise_premarket")

    def get_premarket_progress():
        if env.prem_progress_raises:
            raise env.prem_progress_raises
        return dict(env.prem_progress)

    def request_premarket_stop():
        env.prem_stop_calls += 1
        if env.prem_stop_raises:
            raise env.prem_stop_raises

    def precalculate_surprise_baselines(symbols, force=False):
        env.base_calls.append({"symbols": symbols, "force": force})
        if env.base_raises:
            raise env.base_raises
        return dict(env.base_result)

    def default_universe_from_env():
        return list(env.default_universe)

    for fn in (get_premarket_progress, request_premarket_stop, precalculate_surprise_baselines,
               default_universe_from_env):
        setattr(prem, fn.__name__, fn)

    sch = types.ModuleType("surprise_schema")

    def ensure_surprise_schema():
        env.schema_calls += 1
        if env.schema_raises:
            raise env.schema_raises
        return dict(env.schema)

    sch.ensure_surprise_schema = ensure_surprise_schema

    def build_universe():
        env.universe_calls += 1
        if env.universe_raises:
            raise env.universe_raises
        return list(env.universe)

    monkeypatch.setitem(sys.modules, "ipo_scanner", ipo)
    monkeypatch.setitem(sys.modules, "surprise_premarket", prem)
    monkeypatch.setitem(sys.modules, "surprise_schema", sch)
    monkeypatch.setattr(gw, "_build_scan_universe", build_universe)
    monkeypatch.setattr(gw, "NOTIFICATION_URL", NOTIF)
    # the proxy route caches "schema ready" in a module global; start every test unconfirmed
    monkeypatch.setattr(gw, "_SURPRISE_SCHEMA_CONFIRMED", False, raising=False)
    return env


class Resp:
    def __init__(self, status=200, data=None, json_raises=False):
        self.status_code = status
        self._data = {} if data is None else data
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("not json")
        return self._data


@pytest.fixture
def posts(monkeypatch):
    calls = []
    state = {"out": Resp(200, {"delivered": True})}

    def fake_post(url, **kw):
        calls.append((url, kw))
        if isinstance(state["out"], Exception):
            raise state["out"]
        return state["out"]

    monkeypatch.setattr(httpx, "post", fake_post)
    return types.SimpleNamespace(calls=calls, state=state)


def _missing(monkeypatch, name):
    monkeypatch.setitem(sys.modules, name, None)


# ═════════════════════════════════════════════════════════════════════════════
# /ipo/scan
# ═════════════════════════════════════════════════════════════════════════════

class TestIpoScan:
    def test_running_scan_short_circuits(self, ienv, client):
        ienv.ipo_progress = {"status": "running", "done": 4, "total": 10}
        out = client.post("/ipo/scan").json()
        assert out == {"accepted": True, "already_running": True, "status": "running", "done": 4, "total": 10}
        assert ienv.scan_calls == []

    def test_default_request_starts_a_background_scan(self, ienv, client):
        out = client.post("/ipo/scan").json()
        assert out == {"accepted": True, "background": True, "force": False, "wipe": False,
                       "message": "IPO scan started"}
        assert ienv.scan_calls == [{"force": False, "wipe": False}]

    def test_force_and_wipe_are_passed_to_the_background_task(self, ienv, client):
        out = client.get("/surprise/ipo/scan?force=true&wipe=true").json()
        assert out["force"] is True and out["wipe"] is True
        assert ienv.scan_calls == [{"force": True, "wipe": True}]

    def test_background_false_runs_inline_and_merges_the_result(self, ienv, client):
        out = client.post("/ipo/scan?background=false&force=true").json()
        assert out == {"accepted": True, "background": False, "count": 3}
        assert ienv.scan_calls == [{"force": True, "wipe": False}]

    def test_without_a_background_tasks_object_the_scan_runs_inline(self, ienv):
        out = _run(gw.api_ipo_scan(background=True, force=False, wipe=True, background_tasks=None))
        assert out == {"accepted": True, "background": False, "count": 3}
        assert ienv.scan_calls == [{"force": False, "wipe": True}]

    @pytest.mark.parametrize("progress", [{"status": "idle"}, {"status": "done"}, {}])
    def test_any_non_running_status_allows_a_new_scan(self, ienv, client, progress):
        ienv.ipo_progress = progress
        assert client.post("/ipo/scan").json()["message"] == "IPO scan started"

    def test_inline_result_can_override_the_envelope_keys(self, ienv):
        # NOT FIXED: `**result` is spread last, so a result carrying "accepted" / "background" wins.
        ienv.scan_result = {"accepted": False, "background": "maybe"}
        out = _run(gw.api_ipo_scan(background=False, force=False, wipe=False, background_tasks=None))
        assert out == {"accepted": False, "background": "maybe"}

    def test_missing_module_is_an_unhandled_500(self, client, monkeypatch):
        # NOT FIXED: unlike the sibling IPO routes, the import / progress read are not guarded.
        _missing(monkeypatch, "ipo_scanner")
        assert client.post("/ipo/scan").status_code == 500


# ═════════════════════════════════════════════════════════════════════════════
# status / list / audit / stop
# ═════════════════════════════════════════════════════════════════════════════

class TestIpoStatusListAuditStop:
    def test_status_passes_the_progress_through_on_both_paths(self, ienv, client):
        ienv.ipo_progress = {"status": "running", "done": 1}
        assert client.get("/ipo/status").json() == {"status": "running", "done": 1}
        assert client.get("/surprise/ipo/status").json() == {"status": "running", "done": 1}

    def test_status_failure_is_an_error_envelope(self, ienv, client):
        ienv.ipo_progress_raises = RuntimeError("p" * 300)
        assert client.get("/ipo/status").json() == {"status": "error", "message": "p" * 160}

    def test_status_missing_module_is_an_error_envelope(self, client, monkeypatch):
        _missing(monkeypatch, "ipo_scanner")
        assert client.get("/ipo/status").json()["status"] == "error"

    def test_list_defaults_to_no_display_window(self, ienv, client):
        ienv.listing = {"results": [{"symbol": "AAA"}], "generated_at": "now"}
        assert client.get("/ipo/list").json() == ienv.listing
        assert ienv.list_calls == [None]

    def test_list_forwards_display_days(self, ienv, client):
        client.get("/surprise/ipo/list?display_days=365")
        assert ienv.list_calls == [365]

    def test_list_failure_returns_an_empty_result_set(self, ienv, client):
        ienv.list_raises = RuntimeError("l" * 300)
        assert client.get("/ipo/list").json() == {"results": [], "generated_at": None, "error": "l" * 160}

    def test_list_missing_module_returns_an_empty_result_set(self, client, monkeypatch):
        _missing(monkeypatch, "ipo_scanner")
        body = client.get("/ipo/list").json()
        assert body["results"] == [] and body["generated_at"] is None and "error" in body

    def test_audit_passes_through(self, ienv, client):
        assert client.get("/ipo/audit").json() == {"ok": True, "rows": [{"symbol": "A"}]}
        assert client.get("/surprise/ipo/audit").status_code == 200

    def test_audit_failure_is_an_error_envelope(self, ienv, client):
        ienv.audit_raises = RuntimeError("a" * 400)
        assert client.get("/ipo/audit").json() == {"ok": False, "error": "a" * 200, "rows": []}

    def test_stop_requests_the_halt(self, ienv, client):
        out = client.post("/ipo/stop").json()
        assert out == {"ok": True, "message": "Stop requested — will halt after the current symbol."}
        assert client.post("/surprise/ipo/stop").status_code == 200 and ienv.stop_calls == 2

    def test_stop_failure_is_an_error_envelope(self, ienv, client):
        ienv.stop_raises = RuntimeError("s" * 300)
        assert client.post("/ipo/stop").json() == {"ok": False, "error": "s" * 160}


# ═════════════════════════════════════════════════════════════════════════════
# add / repair-batch / purge-non-equity
# ═════════════════════════════════════════════════════════════════════════════

class TestIpoAdd:
    def test_resolved_entry_is_accepted(self, ienv, client):
        out = client.post("/ipo/add", json={"company_name": "Acme Ltd"}).json()
        assert out == {"accepted": True, "entry": {"symbol": "NEW"}} and ienv.add_calls == ["Acme Ltd"]

    def test_unresolved_name_returns_the_message_and_suggestions(self, ienv, client):
        ienv.add_result = {"resolved": False, "message": "not found", "suggestions": ["Acme Inc"]}
        out = client.post("/surprise/ipo/add", json={"company_name": "Acm"}).json()
        assert out == {"accepted": False, "message": "not found", "suggestions": ["Acme Inc"]}

    def test_missing_suggestions_become_an_empty_list(self, ienv, client):
        ienv.add_result = {"resolved": False}
        out = client.post("/ipo/add", json={"company_name": "X"}).json()
        assert out == {"accepted": False, "message": None, "suggestions": []}

    def test_missing_company_name_is_a_422(self, ienv, client):
        assert client.post("/ipo/add", json={}).status_code == 422 and ienv.add_calls == []

    def test_resolver_failure_is_an_unhandled_500(self, ienv, client):
        # NOT FIXED: add_manual_ipo_by_name() (which hits NSE / ipoalerts) is not wrapped, so any upstream
        # error escapes as a generic 500 instead of the accepted=False envelope.
        ienv.add_raises = RuntimeError("nse blocked")
        assert client.post("/ipo/add", json={"company_name": "X"}).status_code == 500


class TestIpoRepairAndPurge:
    def test_repair_defaults(self, ienv, client):
        out = client.post("/ipo/repair-batch").json()
        assert out == {"repaired": 2} and ienv.repair_calls == [{"limit": 15, "symbol": None}]

    def test_repair_forwards_limit_and_symbol(self, ienv, client):
        client.post("/surprise/ipo/repair-batch?limit=7&symbol=AAA")
        assert ienv.repair_calls == [{"limit": 7, "symbol": "AAA"}]

    @pytest.mark.parametrize("limit", [0, 101])
    def test_repair_limit_outside_1_to_100_is_a_422(self, ienv, client, limit):
        assert client.post(f"/ipo/repair-batch?limit={limit}").status_code == 422

    def test_repair_missing_module_is_an_error_envelope(self, client, monkeypatch):
        _missing(monkeypatch, "ipo_scanner")
        out = client.post("/ipo/repair-batch").json()
        assert out["status"] == "error" and out["error"].startswith("ipo_scanner unavailable: ")

    def test_repair_failure_is_an_unhandled_500(self, ienv, client):
        # NOT FIXED: only the import is guarded; an error inside ipo_repair_batch() is a generic 500.
        ienv.repair_raises = RuntimeError("repair down")
        assert client.post("/ipo/repair-batch").status_code == 500

    def test_purge_passes_the_result_through_on_both_paths(self, ienv, client):
        assert client.post("/ipo/purge-non-equity").json() == {"deleted": 4}
        assert client.post("/surprise/ipo/purge-non-equity").json() == {"deleted": 4}

    def test_purge_missing_module_is_an_error_envelope(self, client, monkeypatch):
        _missing(monkeypatch, "ipo_scanner")
        out = client.post("/ipo/purge-non-equity").json()
        assert out["status"] == "error" and out["error"].startswith("ipo_scanner unavailable: ")

    def test_purge_failure_is_an_unhandled_500(self, ienv, client):
        # NOT FIXED: same as repair — the purge call itself is outside the try.
        ienv.purge_raises = RuntimeError("db down")
        assert client.post("/ipo/purge-non-equity").status_code == 500


# ═════════════════════════════════════════════════════════════════════════════
# /ipo/notify-top-picks
# ═════════════════════════════════════════════════════════════════════════════

def _ipo(sym, **kw):
    d = {"symbol": sym, "decision": "BUY NOW", "ipo_score": 78, "stage": "listing_day",
         "issue_price": 100, "current_price": 112.5, "current_vs_issue_pct": 12.5}
    d.update(kw)
    return d


def _msg(posts):
    return posts.calls[0][1]["json"]["message"]


class TestIpoNotifyTopPicks:
    def test_message_format_and_post_arguments(self, ienv, client, posts):
        ienv.listing = {"results": [
            _ipo("AAA"),
            _ipo("BBB", decision="PREPARE TO BUY", ipo_score=None, pre_listing_advisory_score=64,
                 stage="pre_listing", issue_price=50, current_price=48, current_vs_issue_pct=-4),
        ]}
        out = client.post("/ipo/notify-top-picks").json()
        assert out["ok"] is True and out["sent"] is True and out["count"] == 2
        assert out["symbols"] == ["AAA", "BBB"] and out["notification_result"] == {"delivered": True}
        (url, kw), = posts.calls
        assert url == f"{NOTIF}/notify" and kw["timeout"] == 15
        assert kw["json"]["title"] == "IPO Tracker — Top Picks" and kw["json"]["channel"] == "telegram"
        assert kw["json"]["message"] == (
            "🆕 *IPO Tracker — Top 2*\n"
            "1. AAA — BUY NOW (score 78/100, listing day)\n   Issue ₹100 → Current ₹112.5 (+12.50%)\n"
            "2. BBB — PREPARE TO BUY (score 64/100, pre listing)\n   Issue ₹50 → Current ₹48 (-4.00%)")
        assert ienv.list_calls == [None]

    def test_no_results_means_nothing_is_sent(self, ienv, client, posts):
        out = client.post("/surprise/ipo/notify-top-picks").json()
        assert out == {"ok": False, "sent": False, "count": 0,
                       "message": "No IPO Tracker results available right now — run Scan IPOs first."}
        assert posts.calls == []

    @pytest.mark.parametrize("listing", [{}, {"results": None}])
    def test_missing_results_key_is_the_same(self, ienv, client, posts, listing):
        ienv.listing = listing
        assert client.post("/ipo/notify-top-picks").json()["count"] == 0

    def test_list_failure_is_a_500(self, ienv, client, posts):
        ienv.list_raises = RuntimeError("db down")
        r = client.post("/ipo/notify-top-picks")
        assert r.status_code == 500 and r.json()["detail"] == "ipo_scanner import failed: db down"

    def test_missing_module_is_a_500(self, client, posts, monkeypatch):
        _missing(monkeypatch, "ipo_scanner")
        r = client.post("/ipo/notify-top-picks")
        assert r.status_code == 500 and r.json()["detail"].startswith("ipo_scanner import failed")

    def test_missing_decision_and_scores_are_dashes(self, ienv, client, posts):
        ienv.listing = {"results": [{"symbol": "AAA", "issue_price": 10}]}
        client.post("/ipo/notify-top-picks")
        assert _msg(posts).endswith("1. AAA — — (score —/100, )\n   Issue ₹10 → Current ₹— (—)")

    def test_a_zero_ipo_score_falls_through_to_the_advisory_score(self, ienv, client, posts):
        # NOT FIXED: `ipo_score or pre_listing_advisory_score` treats a real 0 as missing, so a scored-0
        # IPO shows the advisory score (or a dash) instead.
        ienv.listing = {"results": [_ipo("AAA", ipo_score=0, pre_listing_advisory_score=55),
                                    _ipo("BBB", ipo_score=0)]}
        client.post("/ipo/notify-top-picks")
        assert "AAA — BUY NOW (score 55/100" in _msg(posts) and "BBB — BUY NOW (score —/100" in _msg(posts)

    @pytest.mark.parametrize("chg,shown", [(None, "—"), ("abc", "—"), (3.5, "+3.50%"), (-2, "-2.00%"), (0, "+0.00%")])
    def test_change_formatting(self, ienv, client, posts, chg, shown):
        ienv.listing = {"results": [_ipo("AAA", current_vs_issue_pct=chg)]}
        client.post("/ipo/notify-top-picks")
        assert f"Current ₹112.5 ({shown})" in _msg(posts)

    def test_missing_issue_price_is_printed_as_none(self, ienv, client, posts):
        # NOT FIXED: only the current price gets a dash fallback; a missing issue price shows "₹None".
        ienv.listing = {"results": [_ipo("AAA", issue_price=None)]}
        client.post("/ipo/notify-top-picks")
        assert "Issue ₹None → Current" in _msg(posts)

    def test_top_n_defaults_to_five_and_is_clamped(self, ienv, client, posts):
        ienv.listing = {"results": [_ipo(f"S{i}") for i in range(8)]}
        assert client.post("/ipo/notify-top-picks").json()["count"] == 5
        assert client.post("/ipo/notify-top-picks?top_n=2").json()["count"] == 2
        assert client.post("/ipo/notify-top-picks?top_n=20").json()["count"] == 8

    @pytest.mark.parametrize("top_n", [0, 21])
    def test_top_n_outside_1_to_20_is_a_422(self, ienv, client, posts, top_n):
        assert client.post(f"/ipo/notify-top-picks?top_n={top_n}").status_code == 422
        assert posts.calls == []

    @pytest.mark.parametrize("reply,sent", [
        (Resp(200, {"delivered": True}), True),
        (Resp(200, {"delivered": False}), False),
        (Resp(200, {}), False),
    ])
    def test_sent_reflects_the_delivered_flag(self, ienv, client, posts, reply, sent):
        ienv.listing = {"results": [_ipo("AAA")]}
        posts.state["out"] = reply
        out = client.post("/ipo/notify-top-picks").json()
        assert out["ok"] is True and out["sent"] is sent and out["notification_result"] == reply._data

    def test_non_json_reply_falls_back_to_the_status_code(self, ienv, client, posts):
        ienv.listing = {"results": [_ipo("AAA")]}
        posts.state["out"] = Resp(502, json_raises=True)
        out = client.post("/ipo/notify-top-picks").json()
        assert out["sent"] is False and out["notification_result"] == {"status_code": 502}

    def test_non_dict_reply_is_not_delivered(self, ienv, client, posts):
        ienv.listing = {"results": [_ipo("AAA")]}
        posts.state["out"] = Resp(200, ["delivered"])
        assert client.post("/ipo/notify-top-picks").json()["sent"] is False

    def test_post_failure_is_a_200_error_envelope(self, ienv, client, posts):
        ienv.listing = {"results": [_ipo("AAA"), _ipo("BBB")]}
        posts.state["out"] = httpx.ConnectError("c" * 400)
        r = client.post("/ipo/notify-top-picks")
        body = r.json()
        assert r.status_code == 200 and body["ok"] is False and body["sent"] is False and body["count"] == 2
        assert body["error"] == ("c" * 400)[:300]


# ═════════════════════════════════════════════════════════════════════════════
# premarket status / stop
# ═════════════════════════════════════════════════════════════════════════════

class TestPremarketStatusAndStop:
    def test_status_passes_through_on_both_paths(self, ienv, client):
        ienv.prem_progress = {"is_running": True, "percent": 40}
        assert client.get("/surprise/premarket/status").json() == {"is_running": True, "percent": 40}
        assert client.get("/api/surprise/premarket/status").status_code == 200

    def test_status_failure_is_an_error_snapshot(self, ienv, client):
        ienv.prem_progress_raises = RuntimeError("f" * 300)
        assert client.get("/surprise/premarket/status").json() == {
            "is_running": False, "stage": "error", "percent": 0, "error": "f" * 160, "message": "f" * 160}

    def test_status_missing_module_is_an_error_snapshot(self, client, monkeypatch):
        _missing(monkeypatch, "surprise_premarket")
        body = client.get("/surprise/premarket/status").json()
        assert body["is_running"] is False and body["stage"] == "error"

    def test_stop_requests_the_halt_on_both_paths(self, ienv, client):
        out = client.post("/surprise/stop").json()
        assert out == {"ok": True, "message": "Stop requested — will halt after the current symbol/chunk."}
        assert client.post("/api/surprise/stop").status_code == 200 and ienv.prem_stop_calls == 2

    def test_stop_failure_is_an_error_envelope(self, ienv, client):
        ienv.prem_stop_raises = RuntimeError("s" * 300)
        assert client.post("/surprise/stop").json() == {"ok": False, "error": "s" * 160}


# ═════════════════════════════════════════════════════════════════════════════
# /surprise/premarket (proxy)
# ═════════════════════════════════════════════════════════════════════════════

class Req:
    """Just enough of a starlette Request for the premarket proxy."""

    def __init__(self, body=b"", query=None):
        self._body = body
        self.query_params = dict(query or {})

    async def body(self):
        return self._body


def _premarket(body=b"", **query):
    return _run(gw.api_surprise_premarket_proxy(Req(body, query)))


def _jbody(symbols):
    return json.dumps({"symbols": symbols}).encode()


@pytest.fixture
def threads(monkeypatch):
    created = []

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None, **kw):
            self.target, self.daemon, self.name, self.started = target, daemon, name, False
            created.append(self)

        def start(self):
            self.started = True

    monkeypatch.setattr(threading, "Thread", FakeThread)
    return created


class TestPremarketSchemaGate:
    def test_schema_is_ensured_once_then_remembered(self, ienv):
        first = _premarket(_jbody(["A"]), background="false")
        assert ienv.schema_calls == 1 and gw._SURPRISE_SCHEMA_CONFIRMED is True
        assert first["schema"] == {"ok": True, "backend": "postgres"}
        second = _premarket(_jbody(["A"]), background="false")
        assert ienv.schema_calls == 1
        assert second["schema"] == {"ok": True, "table": "surprise_static_feed", "cached": True}

    def test_a_missing_confirmed_flag_is_treated_as_false(self, ienv, monkeypatch):
        monkeypatch.delattr(gw, "_SURPRISE_SCHEMA_CONFIRMED", raising=False)
        _premarket(_jbody(["A"]), background="false")
        assert ienv.schema_calls == 1 and gw._SURPRISE_SCHEMA_CONFIRMED is True

    @pytest.mark.parametrize("backend,fragment", [
        ("oracle", "Set ORACLE_DSN + wallet env on api-gateway (Oracle deployment)"),
        ("postgres", "Set DATABASE_URL or CACHE_DATABASE_URL on api-gateway to Neon pooler URL"),
        (None, "Set DATABASE_URL or CACHE_DATABASE_URL on api-gateway to Neon pooler URL"),
    ])
    def test_unready_schema_is_a_503_with_a_backend_specific_hint(self, ienv, backend, fragment):
        ienv.schema = {"ok": False, "backend": backend, "error": "no conn"}
        resp = _premarket(_jbody(["A"]))
        body = json.loads(resp.body)
        assert resp.status_code == 503
        assert body == {"ok": False, "error": "schema_failed", "message": fragment, "schema": ienv.schema}
        assert gw._SURPRISE_SCHEMA_CONFIRMED is False and ienv.base_calls == []

    def test_schema_exception_is_a_503_with_a_truncated_error(self, ienv):
        ienv.schema_raises = RuntimeError("e" * 400)
        resp = _premarket(_jbody(["A"]))
        body = json.loads(resp.body)
        assert resp.status_code == 503 and body["schema"] == {"ok": False, "error": "e" * 200}

    def test_missing_schema_module_is_a_503(self, ienv, monkeypatch):
        _missing(monkeypatch, "surprise_schema")
        assert _premarket(_jbody(["A"])).status_code == 503

    def test_an_unready_schema_is_rechecked_on_the_next_call(self, ienv):
        ienv.schema = {"ok": False}
        _premarket()
        _premarket()
        assert ienv.schema_calls == 2

    def test_missing_premarket_module_escapes_as_an_import_error(self, ienv, monkeypatch):
        # NOT FIXED: the surprise_premarket import comes after the guarded schema step and is unguarded.
        _missing(monkeypatch, "surprise_premarket")
        with pytest.raises(ImportError):
            _premarket(_jbody(["A"]))


class TestPremarketSymbolParsing:
    def test_body_symbols_are_used_verbatim(self, ienv):
        _premarket(_jbody(["aaa", "BBB.NS"]), background="false")
        assert ienv.base_calls == [{"symbols": ["aaa", "BBB.NS"], "force": False}] and ienv.universe_calls == 0

    def test_query_symbols_are_split_and_trimmed(self, ienv):
        _premarket(b"", background="false", symbols="a, b ,,c")
        assert ienv.base_calls[0]["symbols"] == ["a", "b", "c"]

    def test_empty_body_list_falls_back_to_the_query(self, ienv):
        _premarket(_jbody([]), background="false", symbols="q1,q2")
        assert ienv.base_calls[0]["symbols"] == ["q1", "q2"]

    def test_non_list_body_symbols_are_ignored(self, ienv):
        _premarket(_jbody("AAA"), background="false", symbols="q1")
        assert ienv.base_calls[0]["symbols"] == ["q1"]

    @pytest.mark.parametrize("raw", [b"{not json", b'["AAA"]', b'"text"', b"null"])
    def test_unusable_bodies_fall_back_to_the_universe(self, ienv, raw):
        _premarket(raw, background="false")
        assert ienv.universe_calls == 1 and ienv.base_calls[0]["symbols"] == ["UNI1", "UNI2"]

    @pytest.mark.parametrize("value,expected", [("true", True), ("1", True), ("yes", True), ("YES", True),
                                                 ("", True), ("false", False), ("0", False), ("no", False),
                                                 ("later", False)])
    def test_force_and_background_flag_parsing(self, ienv, threads, value, expected):
        _premarket(_jbody(["A"]), background=value)
        assert bool(threads) is expected                                   # a thread only when background
        _premarket(_jbody(["A"]), background="false", force=value)
        assert ienv.base_calls[-1]["force"] is (value.lower() in ("1", "true", "yes"))


class TestPremarketAlreadyRunning:
    def test_running_job_is_reported_without_starting_another(self, ienv, threads):
        ienv.prem_progress = {"is_running": True, "percent": 10}
        out = _premarket(_jbody(["A"]))
        assert out == {"ok": True, "accepted": False, "already_running": True,
                       "message": "Premarket already running — poll /surprise/premarket/status",
                       "progress": {"is_running": True, "percent": 10},
                       "schema": {"ok": True, "backend": "postgres"}}
        assert threads == [] and ienv.base_calls == []

    def test_synchronous_call_resolves_the_universe_before_noticing_the_running_job(self, ienv):
        # NOT FIXED: the already-running check comes after the (slow, NSE-backed) universe injection.
        ienv.prem_progress = {"is_running": True}
        _premarket(b"", background="false")
        assert ienv.universe_calls == 1 and ienv.base_calls == []


class TestPremarketBackground:
    def test_explicit_symbols_start_a_named_daemon_thread(self, ienv, threads):
        out = _premarket(_jbody(["A", "B"]))
        assert out == {
            "ok": True, "accepted": True, "background": True, "symbols": 2, "universe_injected": 2,
            "runner": "api-gateway", "backend": "postgres",
            "message": "Premarket started on gateway (Neon) — poll /surprise/premarket/status",
            "progress": {"is_running": False}, "schema": {"ok": True, "backend": "postgres"}}
        (t,) = threads
        assert t.started is True and t.daemon is True and t.name == "gw-surprise-premarket"
        assert ienv.base_calls == [] and ienv.universe_calls == 0                 # nothing ran on the request
        t.target()
        assert ienv.base_calls == [{"symbols": ["A", "B"], "force": False}]

    def test_oracle_backend_is_named_in_the_message(self, ienv, threads):
        ienv.schema = {"ok": True, "backend": "oracle"}
        out = _premarket(_jbody(["A"]))
        assert out["message"] == "Premarket started on gateway (Oracle ADB) — poll /surprise/premarket/status"
        assert out["backend"] == "oracle"

    def test_force_is_forwarded_to_the_job(self, ienv, threads):
        _premarket(_jbody(["A"]), force="true")
        threads[0].target()
        assert ienv.base_calls[0]["force"] is True

    def test_without_symbols_the_universe_is_resolved_inside_the_job(self, ienv, threads):
        ienv.universe = ["aaa.ns", "BBB.BO", " ccc ", "", None]
        out = _premarket()
        assert out["symbols"] is None and out["universe_injected"] == "resolving in background"
        assert ienv.universe_calls == 0
        threads[0].target()
        assert ienv.universe_calls == 1
        assert ienv.base_calls == [{"symbols": ["AAA", "BBB", "CCC"], "force": False}]

    @pytest.mark.parametrize("universe,raises", [([], None), ([], "boom")])
    def test_job_falls_back_to_the_default_universe(self, ienv, threads, universe, raises):
        ienv.universe = universe
        if raises:
            ienv.universe_raises = RuntimeError(raises)
        _premarket()
        threads[0].target()
        assert ienv.base_calls[0]["symbols"] == ["DEF1", "DEF2"]

    def test_a_universe_of_bare_suffixes_becomes_a_list_with_one_empty_symbol(self, ienv, threads):
        # NOT FIXED: entries are filtered before they are normalised, so ".NS" survives the filter and
        # normalises to "" — the list is non-empty, the default-universe fallback never fires, and the
        # baseline job is handed [""].
        ienv.universe = [".NS"]
        _premarket()
        threads[0].target()
        assert ienv.base_calls[0]["symbols"] == [""]

    def test_a_failing_baseline_run_is_swallowed_by_the_job(self, ienv, threads):
        ienv.base_raises = RuntimeError("yfinance down")
        _premarket(_jbody(["A"]))
        threads[0].target()                              # must not raise out of the thread
        assert len(ienv.base_calls) == 1


class TestPremarketSynchronous:
    def test_explicit_symbols_run_inline_and_the_result_is_decorated(self, ienv, threads):
        out = _premarket(_jbody(["A", "B"]), background="false", force="1")
        assert out == {"ok": True, "processed": 2, "runner": "api-gateway",
                       "schema": {"ok": True, "backend": "postgres"}}
        assert ienv.base_calls == [{"symbols": ["A", "B"], "force": True}] and threads == []

    def test_no_symbols_inject_the_normalised_universe(self, ienv):
        ienv.universe = ["aaa.ns", "BBB.BO", " ccc ", "", None]
        _premarket(b"", background="false")
        assert ienv.base_calls[0]["symbols"] == ["AAA", "BBB", "CCC"]

    @pytest.mark.parametrize("universe,raises", [([], None), ([], "boom")])
    def test_empty_or_failing_universe_uses_the_default_universe(self, ienv, universe, raises):
        ienv.universe = universe
        if raises:
            ienv.universe_raises = RuntimeError(raises)
        _premarket(b"", background="false")
        assert ienv.base_calls[0]["symbols"] == ["DEF1", "DEF2"]

    def test_a_universe_of_bare_suffixes_skips_the_default_fallback(self, ienv):
        # NOT FIXED: same filter-before-normalise gap as the background job — [".NS"] becomes [""].
        ienv.universe = [".NS"]
        _premarket(b"", background="false")
        assert ienv.base_calls[0]["symbols"] == [""]

    def test_baseline_failure_propagates_on_the_synchronous_path(self, ienv):
        # NOT FIXED: only the background job swallows baseline errors; the inline call raises (a 500).
        ienv.base_raises = RuntimeError("yfinance down")
        with pytest.raises(RuntimeError):
            _premarket(_jbody(["A"]), background="false")
