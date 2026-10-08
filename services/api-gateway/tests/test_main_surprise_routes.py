"""tests/test_main_surprise_routes.py — coverage for api-gateway/main.py, slice 11 (lines 7381-7776)

Pass 69. The "Surprise momentum scanner" route block, up to the end of the Telegram push:

* `POST|GET /surprise/run-premarket-feed` — body / universe symbol selection, the 200-symbol cap, 500 mapping;
* `POST /surprise/repair-batch` — `limit` validation, threaded call, 500 mapping;
* `GET /surprise/audit` — the 20 s memo (`cached` flag, TTL 0, stale entry) and the failure envelope;
* `GET /surprise/scan` — symbol parsing, flag pass-through, `limit` truncation, and the
  `SURPRISE_SCAN_DEADLINE_S` stale-result fallback (served stale / waits when it cannot);
* `GET /surprise/scan/stream` — the NDJSON generator (empty static feed, universe / symbol selection,
  quote failures, chunking, progress + ETA, the final `done` line);
* `GET /surprise/static` — row cap and JSON-safe values;
* `POST /surprise/notify-top-picks` — message format, `top_n`, delivery result mapping.

`surprise_scanner` is replaced by a fake module in `sys.modules` (the routes import it lazily), the shared
http client and the sync `httpx.post` are faked. Nothing touches the network or a database. Findings are
pinned as current behaviour and marked ``NOT FIXED``.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_surprise_routes.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
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

from fastapi import HTTPException
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

class Engine:
    """Stands in for surprise_scanner.surprise_engine."""

    def __init__(self):
        self.scan_calls = []
        self.scan_result = {"stocks": []}
        self.scan_raises = None
        self.scan_delay = 0.0
        self.scan_finished = False
        self.static_cache = {}
        self.static_n = None            # None -> len(static_cache)
        self.load_calls = []
        self.load_raises = None
        self.quotes = {}                # sym -> tick | Exception | None (default: a live tick)
        self.quote_calls = []
        self.quote_sync_raises = False
        self.prime_calls = []           # group242: key lists handed to prime_bulk_ticks
        self.prime_raises = None
        self.score_calls = []
        self.scores = {}                # sym -> dict | None (default: a scored dict)
        self._last_result = None
        self._last_scan_ts = time.time()

    async def scan(self, client, market_data_url, symbols, force_reload_static=False, cached=False):
        self.scan_calls.append({"client": client, "market_data_url": market_data_url, "symbols": symbols,
                                "force_reload_static": force_reload_static, "cached": cached})
        if self.scan_delay:
            await asyncio.sleep(self.scan_delay)
        self.scan_finished = True
        if self.scan_raises:
            raise self.scan_raises
        return self.scan_result

    def load_static_cache(self, force=False):
        self.load_calls.append(force)
        if self.load_raises:
            raise self.load_raises
        return len(self.static_cache) if self.static_n is None else self.static_n

    async def prime_bulk_ticks(self, client, url, symbols):
        self.prime_calls.append(list(symbols))
        if self.prime_raises:
            raise self.prime_raises
        return len(symbols)

    def _fetch_quote(self, client, url, sym):
        self.quote_calls.append(sym)
        if self.quote_sync_raises:
            raise RuntimeError("quote factory down")
        return self._quote(sym)

    async def _quote(self, sym):
        v = self.quotes.get(sym, {"price": 1.0})
        if isinstance(v, Exception):
            raise v
        return v

    def score_stock(self, sym, tick):
        self.score_calls.append((sym, tick))
        if sym in self.scores:
            return self.scores[sym]
        return {"symbol": sym, "score": 70}


class SEnv:
    def __init__(self):
        self.engine = Engine()
        self.feed_calls = []
        self.feed_result = {"ok": True}
        self.feed_raises = None
        self.repair_calls = []
        self.repair_result = {"repaired": 3}
        self.repair_raises = None
        self.audit_calls = 0
        self.audit_result = {"ok": True, "health_score": 90}
        self.audit_raises = None
        self.universe_calls = 0
        self.universe = ["U1", "U2"]
        self.universe_raises = None
        self.http = object()


@pytest.fixture
def senv(monkeypatch):
    env = SEnv()
    mod = types.ModuleType("surprise_scanner")
    mod.surprise_engine = env.engine

    async def run_market_aware_surprise_feed(symbols=None, market_data_url="", force=False):
        env.feed_calls.append({"symbols": symbols, "market_data_url": market_data_url, "force": force})
        if env.feed_raises:
            raise env.feed_raises
        return env.feed_result

    def repair_surprise_batch(limit=15, market_data_url="", symbol=None):
        env.repair_calls.append({"limit": limit, "market_data_url": market_data_url, "symbol": symbol})
        if env.repair_raises:
            raise env.repair_raises
        return env.repair_result

    def audit_surprise_feed():
        env.audit_calls += 1
        if env.audit_raises:
            raise env.audit_raises
        return dict(env.audit_result)

    def build_universe():
        env.universe_calls += 1
        if env.universe_raises:
            raise env.universe_raises
        return list(env.universe)

    mod.run_market_aware_surprise_feed = run_market_aware_surprise_feed
    mod.repair_surprise_batch = repair_surprise_batch
    mod.audit_surprise_feed = audit_surprise_feed
    monkeypatch.setitem(sys.modules, "surprise_scanner", mod)
    monkeypatch.setattr(gw, "_build_scan_universe", build_universe)
    monkeypatch.setattr(gw, "_get_http_client", lambda: env.http)
    monkeypatch.setattr(gw, "NOTIFICATION_URL", NOTIF)
    gw._AUDIT_MEMO.clear()
    yield env
    gw._AUDIT_MEMO.clear()


@pytest.fixture
def no_module(monkeypatch):
    """`from surprise_scanner import ...` raises ImportError."""
    monkeypatch.setitem(sys.modules, "surprise_scanner", None)


# ═════════════════════════════════════════════════════════════════════════════
# run-premarket-feed
# ═════════════════════════════════════════════════════════════════════════════

class TestRunPremarketFeed:
    def test_body_symbols_are_used_and_the_universe_is_not_consulted(self, senv, client):
        r = client.post("/surprise/run-premarket-feed", json={"symbols": ["AAA", "BBB"]})
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert senv.feed_calls == [{"symbols": ["AAA", "BBB"], "market_data_url": gw.MARKET_DATA_URL, "force": False}]
        assert senv.universe_calls == 0

    def test_both_post_paths_and_the_api_get_path_exist(self, senv, client):
        assert client.post("/api/surprise/run-premarket-feed", json={"symbols": ["A"]}).status_code == 200
        assert client.get("/api/surprise/run-premarket-feed").status_code == 200
        assert len(senv.feed_calls) == 2

    def test_force_flag_is_passed_through(self, senv, client):
        client.post("/surprise/run-premarket-feed?force=true", json={"symbols": ["A"]})
        assert senv.feed_calls[0]["force"] is True

    def test_no_body_falls_back_to_the_scan_universe(self, senv, client):
        client.get("/api/surprise/run-premarket-feed")
        assert senv.feed_calls[0]["symbols"] == ["U1", "U2"] and senv.universe_calls == 1

    @pytest.mark.parametrize("body", [{"symbols": []}, {"other": 1}, ["AAA"], "text"])
    def test_unusable_bodies_fall_back_to_the_universe(self, senv, client, body):
        client.post("/surprise/run-premarket-feed", json=body)
        assert senv.feed_calls[0]["symbols"] == ["U1", "U2"]

    def test_invalid_json_body_falls_back_to_the_universe(self, senv, client):
        client.post("/surprise/run-premarket-feed", content=b"{not json",
                    headers={"content-type": "application/json"})
        assert senv.feed_calls[0]["symbols"] == ["U1", "U2"]

    def test_universe_is_capped_at_200_symbols(self, senv, client):
        senv.universe = [f"S{i:03d}" for i in range(250)]
        client.get("/api/surprise/run-premarket-feed")
        assert senv.feed_calls[0]["symbols"] == senv.universe[:200]

    def test_failing_universe_passes_none_to_the_feed(self, senv, client):
        senv.universe_raises = RuntimeError("universe down")
        client.get("/api/surprise/run-premarket-feed")
        assert senv.feed_calls[0]["symbols"] is None

    def test_body_symbols_are_validated_and_capped(self, senv, client):
        # FIXED: a caller-supplied value is cleaned (string -> list, upper-cased, de-duplicated) and capped at 200.
        client.post("/surprise/run-premarket-feed", json={"symbols": "AAA"})
        client.post("/surprise/run-premarket-feed", json={"symbols": [f"S{i}" for i in range(300)]})
        assert senv.feed_calls[0]["symbols"] == ["AAA"] and len(senv.feed_calls[1]["symbols"]) == 200

    def test_feed_failure_is_a_500_with_a_truncated_detail(self, senv, client):
        senv.feed_raises = RuntimeError("x" * 500)
        r = client.post("/surprise/run-premarket-feed", json={"symbols": ["A"]})
        assert r.status_code == 500 and r.json()["detail"] == "x" * 240

    def test_missing_scanner_module_is_a_500(self, no_module, client):
        assert client.get("/api/surprise/run-premarket-feed").status_code == 500


# ═════════════════════════════════════════════════════════════════════════════
# repair-batch
# ═════════════════════════════════════════════════════════════════════════════

class TestRepairBatch:
    def test_defaults(self, senv, client):
        r = client.post("/surprise/repair-batch")
        assert r.status_code == 200 and r.json() == {"repaired": 3}
        assert senv.repair_calls == [{"limit": 15, "market_data_url": gw.MARKET_DATA_URL, "symbol": None}]

    def test_limit_and_symbol_are_forwarded(self, senv, client):
        client.post("/api/surprise/repair-batch?limit=5&symbol=AAA")
        assert senv.repair_calls[0]["limit"] == 5 and senv.repair_calls[0]["symbol"] == "AAA"

    @pytest.mark.parametrize("limit", [0, 101, -1])
    def test_limit_outside_1_to_100_is_rejected(self, senv, client, limit):
        assert client.post(f"/surprise/repair-batch?limit={limit}").status_code == 422
        assert senv.repair_calls == []

    @pytest.mark.parametrize("limit", [1, 100])
    def test_limit_bounds_are_inclusive(self, senv, client, limit):
        assert client.post(f"/surprise/repair-batch?limit={limit}").status_code == 200

    def test_failure_is_a_500_with_a_truncated_detail(self, senv, client):
        senv.repair_raises = RuntimeError("y" * 400)
        r = client.post("/surprise/repair-batch")
        assert r.status_code == 500 and r.json()["detail"] == "y" * 240

    def test_missing_scanner_module_is_a_500(self, no_module, client):
        assert client.post("/surprise/repair-batch").status_code == 500


# ═════════════════════════════════════════════════════════════════════════════
# audit
# ═════════════════════════════════════════════════════════════════════════════

class TestSurpriseAudit:
    @pytest.fixture(autouse=True)
    def _ttl(self, monkeypatch):
        monkeypatch.setattr(gw, "AUDIT_TTL_SEC", 20.0)

    def test_first_call_is_fresh_and_the_second_is_memoised(self, senv, client):
        first = client.get("/api/surprise/audit").json()
        second = client.get("/surprise/audit").json()
        assert first == {"ok": True, "health_score": 90, "cached": False}
        assert second["cached"] is True and second["health_score"] == 90 and "cache_age_sec" in second
        assert senv.audit_calls == 1

    def test_a_stale_memo_entry_is_recomputed(self, senv, client):
        gw._AUDIT_MEMO["surprise_audit"] = (time.time() - 1000, {"ok": True, "health_score": 1})
        out = client.get("/surprise/audit").json()
        assert out["health_score"] == 90 and out["cached"] is False and senv.audit_calls == 1

    def test_cache_age_is_reported(self, senv, client):
        gw._AUDIT_MEMO["surprise_audit"] = (time.time() - 5, {"ok": True})
        out = client.get("/surprise/audit").json()
        assert out["cached"] is True and 5.0 <= out["cache_age_sec"] < 15.0

    def test_ttl_zero_disables_the_memo(self, senv, client, monkeypatch):
        monkeypatch.setattr(gw, "AUDIT_TTL_SEC", 0)
        client.get("/surprise/audit")
        out = client.get("/surprise/audit").json()
        assert senv.audit_calls == 2 and out["cached"] is False

    def test_failure_returns_the_empty_health_envelope_and_is_not_memoised(self, senv, client):
        senv.audit_raises = RuntimeError("m" * 300)
        r = client.get("/surprise/audit")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False and body["total_tracked"] == 0 and body["fully_populated"] == 0
        assert body["missing_data"] == 0 and body["health_score"] == 0 and body["incomplete_stocks"] == []
        assert body["message"] == "m" * 200
        senv.audit_raises = None
        assert client.get("/surprise/audit").json()["cached"] is False and senv.audit_calls == 2

    def test_missing_scanner_module_returns_the_envelope(self, no_module, client):
        body = client.get("/surprise/audit").json()
        assert body["ok"] is False and body["health_score"] == 0


# ═════════════════════════════════════════════════════════════════════════════
# scan
# ═════════════════════════════════════════════════════════════════════════════

def _stocks(n):
    return [{"symbol": f"S{i}", "score": 90 - i} for i in range(n)]


class TestSurpriseScan:
    def test_defaults_are_passed_to_the_engine(self, senv, client):
        senv.engine.scan_result = {"stocks": _stocks(2), "total": 2}
        r = client.get("/api/surprise/scan")
        assert r.status_code == 200 and r.json() == {"stocks": _stocks(2), "total": 2}
        assert senv.engine.scan_calls == [{"client": senv.http, "market_data_url": gw.MARKET_DATA_URL,
                                           "symbols": None, "force_reload_static": False, "cached": False}]

    def test_flags_are_passed_through(self, senv, client):
        client.get("/surprise/scan?force_reload=true&cached=true")
        call = senv.engine.scan_calls[0]
        assert call["force_reload_static"] is True and call["cached"] is True

    def test_symbols_split_on_commas_and_semicolons_without_normalising(self, senv, client):
        client.get("/surprise/scan", params={"symbols": " aaa ; bbb,,ccc.NS ,"})
        assert senv.engine.scan_calls[0]["symbols"] == ["aaa", "bbb", "ccc.NS"]

    def test_only_separators_means_an_empty_list(self, senv, client):
        client.get("/surprise/scan", params={"symbols": " ; , "})
        assert senv.engine.scan_calls[0]["symbols"] == []

    def test_limit_truncates_to_the_top_n_without_touching_the_engine_result(self, senv, client):
        senv.engine.scan_result = {"stocks": _stocks(5), "total": 5}
        out = client.get("/surprise/scan?limit=2").json()
        assert out["stocks"] == _stocks(2) and out["total"] == 5
        assert len(senv.engine.scan_result["stocks"]) == 5

    def test_limit_zero_and_negative_limit_both_empty_the_list(self, senv, client):
        # FIXED: `limit` is applied whenever given; 0 no longer means "unlimited".
        senv.engine.scan_result = {"stocks": _stocks(3)}
        assert client.get("/surprise/scan?limit=0").json()["stocks"] == []
        assert client.get("/surprise/scan?limit=-1").json()["stocks"] == []
        assert len(client.get("/surprise/scan").json()["stocks"]) == 3

    def test_limit_with_a_non_list_stocks_value_changes_nothing(self, senv, client):
        senv.engine.scan_result = {"stocks": None, "error": "empty"}
        assert client.get("/surprise/scan?limit=2").json() == {"stocks": None, "error": "empty"}

    def test_missing_scanner_module_is_a_500(self, no_module, client):
        r = client.get("/surprise/scan")
        assert r.status_code == 500 and r.json()["detail"].startswith("surprise_scanner import failed")

    def test_engine_failure_is_a_mapped_500(self, senv, client):
        # FIXED: scan() errors are mapped to an HTTPException with a clear detail, like the sibling routes.
        senv.engine.scan_raises = RuntimeError("scan down")
        r = client.get("/surprise/scan")
        assert r.status_code == 500 and r.json()["detail"] == "surprise scan failed: scan down"


class TestSurpriseScanDeadline:
    @pytest.fixture(autouse=True)
    def _deadline(self, monkeypatch):
        monkeypatch.setattr(gw, "SURPRISE_SCAN_DEADLINE_S", 0.05)

    @staticmethod
    async def _call(**kw):
        out = await gw.api_surprise_scan(**kw)
        await asyncio.sleep(0.3)               # let the shielded background scan finish cleanly
        return out

    def test_slow_scan_with_a_prior_result_serves_it_flagged_stale(self, senv):
        eng = senv.engine
        eng.scan_delay = 0.2
        eng.scan_result = {"stocks": _stocks(3), "fresh": True}
        eng._last_result = {"stocks": _stocks(1), "fresh": False}
        eng._last_scan_ts = time.time() - 100
        out = _run(self._call())
        assert out["fresh"] is False and out["stocks"] == _stocks(1)
        assert out["from_cache"] is True and out["stale"] is True and out["deadline_exceeded"] is True
        assert 100.0 <= out["cache_age_sec"] < 110.0
        assert "stale" not in eng._last_result                 # a copy was served, not the stored dict
        assert eng.scan_finished is True                       # the scan carried on in the background

    def test_stale_result_is_still_truncated_by_limit(self, senv):
        eng = senv.engine
        eng.scan_delay = 0.2
        eng._last_result = {"stocks": _stocks(5)}
        out = _run(self._call(limit=2))
        assert out["stocks"] == _stocks(2) and out["stale"] is True

    def test_missing_scan_timestamp_reports_a_zero_age(self, senv):
        eng = senv.engine
        eng.scan_delay = 0.2
        eng._last_result = {"stocks": []}
        del eng._last_scan_ts
        out = _run(self._call())
        assert out["stale"] is True and out["cache_age_sec"] < 1.0

    def test_slow_scan_for_explicit_symbols_waits_for_the_real_result(self, senv):
        eng = senv.engine
        eng.scan_delay = 0.2
        eng.scan_result = {"stocks": _stocks(2), "fresh": True}
        eng._last_result = {"stocks": [], "fresh": False}
        out = _run(self._call(symbols="aaa,bbb"))
        assert out == {"stocks": _stocks(2), "fresh": True}
        assert eng.scan_calls[0]["symbols"] == ["aaa", "bbb"]

    def test_slow_scan_that_then_fails_is_a_mapped_500(self, senv):
        eng = senv.engine
        eng.scan_delay = 0.2
        eng.scan_raises = RuntimeError("late failure")
        eng._last_result = None
        with pytest.raises(gw.HTTPException) as ei:
            _run(self._call())
        assert ei.value.status_code == 500 and ei.value.detail == "surprise scan failed: late failure"

    def test_slow_first_ever_scan_waits_for_the_result(self, senv):
        eng = senv.engine
        eng.scan_delay = 0.2
        eng.scan_result = {"stocks": _stocks(1), "fresh": True}
        eng._last_result = None
        assert _run(self._call()) == {"stocks": _stocks(1), "fresh": True}

    def test_fast_scan_is_returned_directly(self, senv):
        senv.engine.scan_result = {"stocks": _stocks(1)}
        senv.engine._last_result = {"stocks": [], "old": True}
        assert _run(self._call()) == {"stocks": _stocks(1)}


class TestSurpriseScanSingleFlight:
    """group 175: concurrent default scans share one engine.scan instead of piling up."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.delenv("SURPRISE_SCAN_SINGLE_FLIGHT", raising=False)
        monkeypatch.setattr(gw, "SURPRISE_SCAN_DEADLINE_S", 5.0)
        monkeypatch.setattr(gw, "_surprise_scan_inflight", None)

    @staticmethod
    async def _many(n, **kw):
        outs = await asyncio.gather(*[gw.api_surprise_scan(**kw) for _ in range(n)])
        await asyncio.sleep(0.05)
        return outs

    def test_concurrent_default_calls_run_one_scan(self, senv):
        eng = senv.engine
        eng.scan_delay = 0.1
        eng.scan_result = {"stocks": _stocks(2)}
        outs = _run(self._many(5, cached=True))
        assert len(eng.scan_calls) == 1
        assert all(o == {"stocks": _stocks(2)} for o in outs)
        assert gw._surprise_scan_inflight is None            # cleared once done

    def test_a_later_call_after_completion_starts_a_new_scan(self, senv):
        eng = senv.engine
        eng.scan_delay = 0.01

        async def go():
            await gw.api_surprise_scan()
            await asyncio.sleep(0.05)
            await gw.api_surprise_scan()
        _run(go())
        assert len(eng.scan_calls) == 2

    def test_explicit_symbols_are_never_shared(self, senv):
        eng = senv.engine
        eng.scan_delay = 0.1
        _run(self._many(3, symbols="aaa,bbb"))
        assert len(eng.scan_calls) == 3

    def test_force_reload_is_never_shared(self, senv):
        eng = senv.engine
        eng.scan_delay = 0.1
        _run(self._many(3, force_reload=True))
        assert len(eng.scan_calls) == 3

    def test_switch_off_restores_one_scan_per_request(self, senv, monkeypatch):
        monkeypatch.setenv("SURPRISE_SCAN_SINGLE_FLIGHT", "0")
        eng = senv.engine
        eng.scan_delay = 0.1
        _run(self._many(4))
        assert len(eng.scan_calls) == 4

    def test_slow_scan_past_deadline_is_joined_not_restarted(self, senv, monkeypatch):
        monkeypatch.setattr(gw, "SURPRISE_SCAN_DEADLINE_S", 0.05)
        eng = senv.engine
        eng.scan_delay = 0.3
        eng._last_result = {"stocks": _stocks(1)}
        eng._last_scan_ts = time.time() - 252

        async def go():
            a = await gw.api_surprise_scan(cached=True)      # times out, serves stale, scan keeps running
            b = await gw.api_surprise_scan(cached=True)      # must join, not start a second scan
            await asyncio.sleep(0.4)
            return a, b
        a, b = _run(go())
        assert a["stale"] is True and b["stale"] is True
        assert len(eng.scan_calls) == 1

    def test_failed_scan_is_shared_then_cleared(self, senv):
        eng = senv.engine
        eng.scan_delay = 0.05
        eng.scan_raises = RuntimeError("boom")

        async def go():
            rs = await asyncio.gather(gw.api_surprise_scan(), gw.api_surprise_scan(), return_exceptions=True)
            await asyncio.sleep(0.05)
            return rs
        rs = _run(go())
        assert all(isinstance(r, gw.HTTPException) and r.status_code == 500 for r in rs)
        assert len(eng.scan_calls) == 1 and gw._surprise_scan_inflight is None


# ═════════════════════════════════════════════════════════════════════════════
# scan/stream
# ═════════════════════════════════════════════════════════════════════════════

async def _stream_response(**kw):
    resp = await gw.api_surprise_scan_stream(**kw)
    chunks = [c async for c in resp.body_iterator]
    return resp, chunks


def _stream(**kw):
    resp, chunks = _run(_stream_response(**kw))
    lines = [json.loads(x) for x in "".join(chunks).splitlines() if x.strip()]
    return resp, lines


def _events(lines, name):
    return [x for x in lines if x.get("_meta") and x.get("event") == name]


def _hits(lines):
    return [x for x in lines if not x.get("_meta")]


class _Clock:
    """Advances 5 s per call so elapsed / ETA are non-zero and deterministic in shape."""

    def __init__(self):
        self.t = 1000.0

    def time(self):
        self.t += 5.0
        return self.t


class TestSurpriseScanStream:
    def test_response_headers_and_media_type(self, senv):
        senv.engine.static_cache = {"AAA": {}}
        resp, _ = _stream()
        assert resp.media_type == "application/x-ndjson"
        assert resp.headers["cache-control"] == "no-cache" and resp.headers["x-accel-buffering"] == "no"

    def test_missing_scanner_module_raises_a_500(self, no_module):
        with pytest.raises(HTTPException) as exc:
            _run(gw.api_surprise_scan_stream())
        assert exc.value.status_code == 500 and "import failed" in exc.value.detail

    def test_empty_static_feed_reports_the_error_and_stops(self, senv):
        _, lines = _stream(force_reload=True)
        assert senv.engine.load_calls == [True]
        assert lines == [
            {"_meta": True, "event": "static_loaded", "static_loaded": 0, "total": 0},
            {"_meta": True, "event": "error", "error": "surprise_static_feed empty — run premarket first"},
            {"_meta": True, "event": "done", "hits": 0, "universe": 0, "elapsed": 0},
        ]
        assert senv.engine.quote_calls == []

    def test_static_loaded_count_comes_from_the_loader(self, senv):
        senv.engine.static_cache = {"AAA": {}}
        senv.engine.static_n = 7
        _, lines = _stream()
        assert lines[0] == {"_meta": True, "event": "static_loaded", "static_loaded": 7, "total": 7}
        assert _events(lines, "scan_start") == [{"_meta": True, "event": "scan_start", "total": 1, "static_loaded": 7}]

    def test_universe_is_the_liquid_names_by_default(self, senv):
        senv.engine.static_cache = {"A": {"is_liquid": False}, "B": {"is_liquid": True}, "C": {}}
        _, lines = _stream()
        assert senv.engine.quote_calls == ["B", "C"]
        assert _events(lines, "scan_start")[0]["total"] == 2

    def test_all_illiquid_falls_back_to_the_whole_cache(self, senv):
        senv.engine.static_cache = {"A": {"is_liquid": False}, "B": {"is_liquid": False}}
        _stream()
        assert senv.engine.quote_calls == ["A", "B"]

    def test_symbols_are_normalised_filtered_and_ordered_as_requested(self, senv):
        senv.engine.static_cache = {"AAA": {"is_liquid": False}, "BBB": {}, "CCC": {}}
        _, lines = _stream(symbols="bbb.bo, aaa.ns ,zzz")
        assert senv.engine.quote_calls == ["BBB", "AAA"]          # liquidity is not applied to explicit symbols
        assert _events(lines, "scan_start")[0]["total"] == 2

    def test_symbols_matching_nothing_give_an_empty_universe(self, senv):
        senv.engine.static_cache = {"AAA": {}}
        _, lines = _stream(symbols="zzz")
        assert _events(lines, "scan_start")[0]["total"] == 0 and _events(lines, "progress") == []
        assert _events(lines, "done")[0]["universe"] == 0 and _hits(lines) == []

    def test_only_separators_mean_the_full_universe(self, senv):
        senv.engine.static_cache = {"AAA": {}, "BBB": {}}
        _stream(symbols=" ; ")
        assert senv.engine.quote_calls == ["AAA", "BBB"]

    def test_quote_failures_are_skipped_not_scored(self, senv):
        eng = senv.engine
        eng.static_cache = {"S00": {}, "S01": {}, "S02": {}}
        eng.quotes = {"S00": RuntimeError("quote down"), "S01": None, "S02": {"price": 5}}
        eng.scores = {"S01": None}
        _, lines = _stream()
        # FIXED: a failed quote is counted and skipped; an empty tick is no longer scored into a "hit"
        assert eng.score_calls == [("S02", {"price": 5})]
        done = _events(lines, "done")[0]
        assert done["quotes_ok"] == 1 and done["quotes_failed"] == 2 and done["hits"] == 1 and done["universe"] == 3
        assert [h["symbol"] for h in _hits(lines)] == ["S02"]

    def test_scored_rows_carry_running_progress(self, senv):
        senv.engine.static_cache = {f"S{i:02d}": {} for i in range(3)}
        _, lines = _stream()
        progress = [h["_progress"] for h in _hits(lines)]
        assert [p["hits"] for p in progress] == [1, 2, 3] and [p["quotes_ok"] for p in progress] == [1, 2, 3]
        assert all(p["processed"] == 3 and p["total"] == 3 for p in progress)

    def test_non_json_values_are_stringified(self, senv):
        senv.engine.static_cache = {"S00": {}}
        senv.engine.scores = {"S00": {"symbol": "S00", "when": datetime(2026, 10, 1, 9, 15)}}
        _, lines = _stream()
        assert _hits(lines)[0]["when"] == "2026-10-01 09:15:00"

    def test_quote_factory_failure_skips_the_whole_chunk(self, senv):
        eng = senv.engine
        eng.static_cache = {"S00": {}, "S01": {}}
        eng.quote_sync_raises = True
        _, lines = _stream()
        assert eng.quote_calls == ["S00"]                        # the factory failed on the first call
        assert eng.score_calls == []
        done = _events(lines, "done")[0]
        assert done["quotes_ok"] == 0 and done["quotes_failed"] == 2

    def test_universe_is_processed_in_chunks_of_20(self, senv):
        senv.engine.static_cache = {f"S{i:02d}": {} for i in range(45)}
        _, lines = _stream()
        prog = _events(lines, "progress")
        assert [p["processed"] for p in prog] == [20, 40, 45]
        assert [p["percent"] for p in prog] == [44, 88, 100]
        assert all(p["total"] == 45 and p["hits"] == p["processed"] for p in prog)
        assert prog[-1]["eta_sec"] is None
        assert _events(lines, "done")[0]["hits"] == 45
        assert [h["_progress"]["processed"] for h in _hits(lines)][:21] == [20] * 20 + [40]

    def test_eta_is_estimated_while_the_scan_is_running(self, senv, monkeypatch):
        senv.engine.static_cache = {f"S{i:02d}": {} for i in range(45)}
        monkeypatch.setattr(gw, "time", _Clock())
        _, lines = _stream()
        prog = _events(lines, "progress")
        assert prog[0]["eta_sec"] is not None and prog[0]["eta_sec"] > 0
        assert prog[-1]["eta_sec"] is None

    def test_group242_universe_is_bulk_primed_once_before_any_quote(self, senv):
        senv.engine.static_cache = {"A": {"is_liquid": False}, "B": {"is_liquid": True}, "C": {}}
        _stream()
        assert senv.engine.prime_calls == [["B", "C"]]

    def test_group242_explicit_symbols_are_primed_in_requested_order(self, senv):
        senv.engine.static_cache = {"AAA": {}, "BBB": {}, "CCC": {}}
        _stream(symbols="bbb.bo, aaa.ns ,zzz")
        assert senv.engine.prime_calls == [["BBB", "AAA"]]

    def test_group242_empty_universe_is_not_primed(self, senv):
        _stream(force_reload=True)
        assert senv.engine.prime_calls == []

    def test_group242_prime_failure_falls_back_to_per_symbol_quotes(self, senv):
        senv.engine.static_cache = {"AAA": {}, "BBB": {}}
        senv.engine.prime_raises = RuntimeError("md down")
        _, lines = _stream()
        assert senv.engine.quote_calls == ["AAA", "BBB"]
        assert lines[-1]["event"] == "done" and lines[-1]["quotes_ok"] == 2

    def test_group242_engine_without_prime_method_still_streams(self, senv):
        senv.engine.static_cache = {"AAA": {}}
        cls = type(senv.engine)
        original = cls.prime_bulk_ticks
        del cls.prime_bulk_ticks
        try:
            _, lines = _stream()
        finally:
            cls.prime_bulk_ticks = original
        assert senv.engine.quote_calls == ["AAA"] and lines[-1]["event"] == "done"

    def test_done_line_summarises_the_run(self, senv):
        senv.engine.static_cache = {"AAA": {}, "BBB": {}}
        _, lines = _stream()
        done = lines[-1]
        assert done["event"] == "done" and done["hits"] == 2 and done["universe"] == 2
        assert done["quotes_ok"] == 2 and isinstance(done["elapsed"], float)


# ═════════════════════════════════════════════════════════════════════════════
# static
# ═════════════════════════════════════════════════════════════════════════════

class TestSurpriseStatic:
    def test_rows_are_json_safe_and_the_count_comes_from_the_loader(self, senv, client):
        senv.engine.static_cache = {"AAA": {"symbol": "AAA", "asof": datetime(2026, 10, 1, 9, 15), "px": 1.5}}
        senv.engine.static_n = 42
        body = client.get("/api/surprise/static").json()
        assert body == {"ok": True, "count": 42, "source": "gateway_cache",
                        "rows": [{"symbol": "AAA", "asof": "2026-10-01 09:15:00", "px": 1.5}]}

    @pytest.mark.parametrize("limit,expected", [(500, 200), (200, 200), (10, 10), (0, 1), (-5, 1)])
    def test_limit_is_clamped_between_1_and_200(self, senv, client, limit, expected):
        senv.engine.static_cache = {f"S{i:03d}": {"symbol": f"S{i:03d}"} for i in range(250)}
        assert len(client.get(f"/surprise/static?limit={limit}").json()["rows"]) == expected

    def test_loader_failure_returns_a_200_error_envelope(self, senv, client):
        senv.engine.load_raises = RuntimeError("e" * 300)
        r = client.get("/surprise/static")
        assert r.status_code == 200
        assert r.json() == {"ok": False, "error": "e" * 200, "rows": []}

    def test_missing_scanner_module_returns_the_envelope(self, no_module, client):
        body = client.get("/surprise/static").json()
        assert body["ok"] is False and body["rows"] == []


# ═════════════════════════════════════════════════════════════════════════════
# notify-top-picks
# ═════════════════════════════════════════════════════════════════════════════

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


def _pick(sym, **kw):
    d = {"symbol": sym, "score": 82, "tier": "strong", "price": 100.5, "change_pct": 2.5,
         "target_1": 110, "trailing_stop": 97, "trigger_type": "gap-up"}
    d.update(kw)
    return d


class TestNotifyTopPicks:
    def test_no_stocks_means_nothing_is_sent(self, senv, client, posts):
        senv.engine.scan_result = {"stocks": []}
        out = client.post("/surprise/notify-top-picks").json()
        assert out == {"ok": False, "sent": False, "count": 0,
                       "message": "No Surprise Momentum picks available right now."}
        assert posts.calls == []

    @pytest.mark.parametrize("result", [{}, {"stocks": None}])
    def test_missing_stocks_key_is_the_same(self, senv, client, posts, result):
        senv.engine.scan_result = result
        assert client.post("/surprise/notify-top-picks").json()["count"] == 0

    def test_scan_error_text_is_used_as_the_message(self, senv, client, posts):
        senv.engine.scan_result = {"stocks": [], "error": "static feed empty"}
        assert client.post("/surprise/notify-top-picks").json()["message"] == "static feed empty"

    def test_engine_is_called_for_a_plain_full_scan(self, senv, client, posts):
        senv.engine.scan_result = {"stocks": [_pick("AAA")]}
        client.post("/surprise/notify-top-picks")
        assert senv.engine.scan_calls == [{"client": senv.http, "market_data_url": gw.MARKET_DATA_URL,
                                           "symbols": None, "force_reload_static": False, "cached": False}]

    def test_message_format_and_post_arguments(self, senv, client, posts):
        senv.engine.scan_result = {"stocks": [
            _pick("AAA"),
            _pick("BBB", score=75, tier="good", price=50, change_pct=-1.5, target_1=55, trailing_stop=48,
                  trigger_type="volume"),
        ]}
        out = client.post("/surprise/notify-top-picks").json()
        assert out["ok"] is True and out["sent"] is True and out["count"] == 2
        assert out["symbols"] == ["AAA", "BBB"] and out["notification_result"] == {"delivered": True}
        (url, kw), = posts.calls
        assert url == f"{NOTIF}/notify" and kw["timeout"] == 15
        assert kw["json"]["title"] == "Surprise Momentum — Top Picks" and kw["json"]["channel"] == "telegram"
        assert kw["json"]["message"] == (
            "🎯 *Surprise Momentum — Top 2 Picks*\n"
            "1. AAA — STRONG (score 82/100)\n   ₹100.5 (+2.50%) · Target ₹110 · Stop ₹97\n   gap-up\n"
            "2. BBB — GOOD (score 75/100)\n   ₹50 (-1.50%) · Target ₹55 · Stop ₹48\n   volume")

    @pytest.mark.parametrize("chg", [None, "abc"])
    def test_unparsable_change_is_a_dash(self, senv, client, posts, chg):
        senv.engine.scan_result = {"stocks": [_pick("AAA", change_pct=chg)]}
        client.post("/surprise/notify-top-picks")
        assert "₹100.5 (—) ·" in posts.calls[0][1]["json"]["message"]

    def test_missing_fields_are_printed_as_dashes(self, senv, client, posts):
        # FIXED: absent score / price / target / stop print a dash, never "None" / "₹None".
        senv.engine.scan_result = {"stocks": [{"symbol": "AAA"}]}
        client.post("/surprise/notify-top-picks")
        assert posts.calls[0][1]["json"]["message"].endswith(
            "1. AAA — — (score —/100)\n   — (—) · Target — · Stop —\n   ")

    def test_top_n_defaults_to_five(self, senv, client, posts):
        senv.engine.scan_result = {"stocks": [_pick(f"S{i}") for i in range(8)]}
        out = client.post("/surprise/notify-top-picks").json()
        assert out["count"] == 5 and out["symbols"] == [f"S{i}" for i in range(5)]
        assert posts.calls[0][1]["json"]["message"].startswith("🎯 *Surprise Momentum — Top 5 Picks*")

    def test_top_n_is_honoured_and_limited_by_the_available_picks(self, senv, client, posts):
        senv.engine.scan_result = {"stocks": [_pick("A"), _pick("B")]}
        assert client.post("/surprise/notify-top-picks?top_n=2").json()["count"] == 2
        assert client.post("/surprise/notify-top-picks?top_n=20").json()["count"] == 2

    @pytest.mark.parametrize("top_n", [0, 21, -1])
    def test_top_n_outside_1_to_20_is_rejected(self, senv, client, posts, top_n):
        assert client.post(f"/surprise/notify-top-picks?top_n={top_n}").status_code == 422
        assert senv.engine.scan_calls == [] and posts.calls == []

    @pytest.mark.parametrize("reply,sent", [
        (Resp(200, {"delivered": True}), True),
        (Resp(200, {"delivered": False}), False),
        (Resp(200, {"status": "queued"}), False),
    ])
    def test_sent_reflects_the_delivered_flag(self, senv, client, posts, reply, sent):
        senv.engine.scan_result = {"stocks": [_pick("AAA")]}
        posts.state["out"] = reply
        out = client.post("/surprise/notify-top-picks").json()
        assert out["ok"] is True and out["sent"] is sent and out["notification_result"] == reply._data

    def test_non_json_reply_falls_back_to_the_status_code(self, senv, client, posts):
        senv.engine.scan_result = {"stocks": [_pick("AAA")]}
        posts.state["out"] = Resp(502, json_raises=True)
        out = client.post("/surprise/notify-top-picks").json()
        # FIXED: a non-2xx reply is a failure the caller sees in `ok`.
        assert out["ok"] is False and out["sent"] is False and out["notification_result"] == {"status_code": 502}

    def test_non_dict_reply_is_not_delivered(self, senv, client, posts):
        senv.engine.scan_result = {"stocks": [_pick("AAA")]}
        posts.state["out"] = Resp(200, ["delivered"])
        out = client.post("/surprise/notify-top-picks").json()
        assert out["sent"] is False and out["notification_result"] == ["delivered"]

    def test_http_error_reply_is_reported_as_not_ok(self, senv, client, posts):
        # FIXED: a 4xx/5xx JSON reply from the notification service now sets ok=False with an error.
        senv.engine.scan_result = {"stocks": [_pick("AAA")]}
        posts.state["out"] = Resp(500, {"detail": "boom"})
        out = client.post("/surprise/notify-top-picks").json()
        assert out["ok"] is False and out["sent"] is False and "HTTP 500" in out["error"]

    def test_post_failure_is_a_200_error_envelope(self, senv, client, posts):
        senv.engine.scan_result = {"stocks": [_pick("AAA"), _pick("BBB")]}
        posts.state["out"] = httpx.ConnectError("c" * 400)
        r = client.post("/surprise/notify-top-picks")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False and body["sent"] is False and body["count"] == 2
        assert body["error"] == ("c" * 400)[:300]

    def test_missing_scanner_module_is_a_500(self, no_module, client, posts):
        r = client.post("/surprise/notify-top-picks")
        assert r.status_code == 500 and r.json()["detail"].startswith("surprise_scanner import failed")

    def test_scan_failure_is_a_mapped_500(self, senv, client, posts):
        # FIXED: a scan error is mapped to an HTTPException with a clear detail (like the sibling routes).
        senv.engine.scan_raises = RuntimeError("scan down")
        r = client.post("/surprise/notify-top-picks")
        assert r.status_code == 500 and r.json()["detail"] == "surprise scan failed: scan down"
        assert posts.calls == []

    def test_a_non_dict_scan_result_is_treated_as_empty(self, senv, client, posts):
        senv.engine.scan_result = ["junk"]
        assert client.post("/surprise/notify-top-picks").status_code == 200
        assert posts.calls == []


class TestPass82CleanSymbolList:
    def test_accepts_strings_and_iterables_and_cleans_them(self):
        assert gw._clean_symbol_list("aaa; bbb,aaa , ") == ["AAA", "BBB"]
        assert gw._clean_symbol_list([" tcs ", "TCS", 5, None, "", "x" * 40, "infy"]) == ["TCS", "INFY"]
        assert gw._clean_symbol_list(("a", "b")) == ["A", "B"]
        assert sorted(gw._clean_symbol_list({"a", "b"})) == ["A", "B"]

    def test_caps_the_size_and_falls_back_to_none(self):
        assert len(gw._clean_symbol_list([f"S{i}" for i in range(500)])) == gw.SYMBOL_LIST_CAP
        assert len(gw._clean_symbol_list([f"S{i}" for i in range(10)], cap=3)) == 3
        for bad in (None, 5, {}, [], [None, 1], "  ,; "):
            assert gw._clean_symbol_list(bad) is None
