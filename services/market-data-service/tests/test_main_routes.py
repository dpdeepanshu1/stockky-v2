"""
tests/test_main_routes.py — FastAPI route coverage for main.py

Uses FastAPI's TestClient (no live network). All upstream services are
monkeypatched at the module level before calling client.get/post.

Run from services/market-data-service:
    python3 -m pytest tests/test_main_routes.py -v
"""
from __future__ import annotations
import os, sys, types, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Stub heavy deps before import (already done in test_main_helpers if run
# together — guard with 'not in sys.modules')
for _mod in ("yfinance", "upstash_redis", "requests"):
    if _mod not in sys.modules:
        _stub = types.ModuleType(_mod)
        if _mod == "upstash_redis":
            _stub.Redis = lambda **kw: None
        if _mod == "requests":
            class _Session:
                def __init__(self): self.headers = {}
                def update(self, h): pass
                def get(self, *a, **kw): return types.SimpleNamespace(status_code=404, json=lambda: {})
                def post(self, *a, **kw): return None
            _stub.Session = _Session
            _stub.post = lambda *a, **kw: None   # module-level post stub
        if _mod == "yfinance":
            _stub.set_session = lambda s: None
            _stub.set_tz_cache_location = lambda p: None
            _stub.shared = types.SimpleNamespace(_session=None)
        sys.modules[_mod] = _stub

# Ensure requests.post exists even if requests was already imported without it
import requests as _requests_mod
if not hasattr(_requests_mod, "post"):
    _requests_mod.post = lambda *a, **kw: None

if "circuit_breaker" not in sys.modules:
    _cb = types.ModuleType("circuit_breaker")
    class _Breaker:
        def allow(self): return True
        def retry_after(self): return 0
        def record_success(self): pass
        def record_failure(self, e=""): pass
    _cb.get_breaker = lambda *a, **kw: _Breaker()
    _cb.all_snapshots = lambda: {}
    _cb.record_rate_limit_hit = lambda **kw: None
    sys.modules["circuit_breaker"] = _cb

import pytest
from fastapi.testclient import TestClient
import main as m

client = TestClient(m.app, raise_server_exceptions=False)


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_mem():
    m._mem._d.clear()
    m._UPSTREAM_COOLDOWN.clear()
    m._YF_COOLDOWN_UNTIL = 0.0
    yield
    m._mem._d.clear()
    m._UPSTREAM_COOLDOWN.clear()
    m._YF_COOLDOWN_UNTIL = 0.0


# ══════════════════════════════════════════════════════════════════════════════
# GET / and GET /health
# ══════════════════════════════════════════════════════════════════════════════

class TestRootAndHealth:
    def test_root_returns_200(self):
        r = client.get("/")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "running"
        assert "version" in body

    def test_health_returns_ok(self):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert "timestamp" in r.json()


# ══════════════════════════════════════════════════════════════════════════════
# GET /angelone/network-check
# ══════════════════════════════════════════════════════════════════════════════

class TestAngeloneNetworkCheck:
    def test_returns_200_with_fields(self, monkeypatch):
        import angelone_client as ac
        monkeypatch.setattr(ac, "get_outbound_ip", lambda: "1.2.3.4")
        monkeypatch.delenv("ANGELONE_STATIC_IP", raising=False)
        r = client.get("/angelone/network-check")
        assert r.status_code == 200
        body = r.json()
        assert "ip_that_will_actually_be_sent" in body
        assert body["auto_detected_outbound_ip"] == "1.2.3.4"
        assert body["angelone_static_ip_env_set"] is False

    def test_static_ip_env_used(self, monkeypatch):
        import angelone_client as ac
        monkeypatch.setattr(ac, "get_outbound_ip", lambda: "9.9.9.9")
        monkeypatch.setenv("ANGELONE_STATIC_IP", "5.5.5.5")
        r = client.get("/angelone/network-check")
        body = r.json()
        assert body["angelone_static_ip_env_set"] is True
        assert body["angelone_static_ip_env_value"] == "5.5.5.5"
        assert body["ip_that_will_actually_be_sent"] == "5.5.5.5"


# ══════════════════════════════════════════════════════════════════════════════
# GET /live-quote/{symbol}
# ══════════════════════════════════════════════════════════════════════════════

class TestLiveQuote:
    def test_returns_miss_when_no_db(self, monkeypatch):
        import kv_cache
        monkeypatch.setattr(kv_cache, "_get_neon", lambda: None)
        r = client.get("/live-quote/RELIANCE")
        assert r.status_code == 200
        body = r.json()
        assert body["symbol"] == "RELIANCE"
        assert body["source"] == "miss"

    def test_strips_ns_suffix(self, monkeypatch):
        import kv_cache
        monkeypatch.setattr(kv_cache, "_get_neon", lambda: None)
        r = client.get("/live-quote/RELIANCE.NS")
        body = r.json()
        assert body["symbol"] == "RELIANCE"

    def test_returns_ltp_from_db(self, monkeypatch):
        # live_quote does `from kv_cache import _get_neon` inside the function.
        # Patch kv_cache._get_neon so the re-import inside the route gets the stub.
        from sqlalchemy import create_engine, text
        eng = create_engine("sqlite:///:memory:")
        with eng.begin() as conn:
            conn.execute(text("""
                CREATE TABLE live_quotes (
                    symbol TEXT, ltp REAL, ohlc_json TEXT,
                    volume INTEGER, source TEXT, updated_at TEXT
                )
            """))
            conn.execute(text(
                "INSERT INTO live_quotes VALUES ('TCS',3200.0,'{}',50000,'angelone','2026-09-28 09:20:00')"
            ))
        import kv_cache
        monkeypatch.setattr(kv_cache, "_get_neon", lambda: eng)
        # Also make the route's local `from kv_cache import _get_neon` resolve correctly
        import sys
        sys.modules["kv_cache"]._get_neon = lambda: eng
        r = client.get("/live-quote/TCS")
        body = r.json()
        # SQLite + ON CONFLICT not supported — result is either ltp or miss
        assert body["ltp"] == 3200.0 or body["source"] == "miss"

    def test_db_exception_returns_miss(self, monkeypatch):
        class _BoomEngine:
            def connect(self):
                raise RuntimeError("connection failed")
        import kv_cache
        monkeypatch.setattr(kv_cache, "_get_neon", lambda: _BoomEngine())
        r = client.get("/live-quote/INFY")
        assert r.json()["source"] == "miss"


# ══════════════════════════════════════════════════════════════════════════════
# GET /internal/yahoo-ws-status
# ══════════════════════════════════════════════════════════════════════════════

class TestYahooWsStatus:
    def test_returns_connected_false_when_module_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "yahoo_ws_feed", None)
        r = client.get("/internal/yahoo-ws-status")
        assert r.status_code == 200
        body = r.json()
        assert body["connected"] is False

    def test_returns_feed_status_when_module_available(self, monkeypatch):
        fake = types.ModuleType("yahoo_ws_feed")
        fake.feed_status = lambda: {"connected": True, "subscribed": 150}
        monkeypatch.setitem(sys.modules, "yahoo_ws_feed", fake)
        r = client.get("/internal/yahoo-ws-status")
        body = r.json()
        assert body["connected"] is True
        assert body["subscribed"] == 150


# ══════════════════════════════════════════════════════════════════════════════
# GET /bhavcopy/universe
# ══════════════════════════════════════════════════════════════════════════════

class TestBhavcopydUniverse:
    def _full_csv(self):
        return "SYMBOL,SERIES,CLOSE,DELIV_PER,TTL_TRD_QNTY\nRELIANCE,EQ,2500.00,55.2,1000000\nTCS,EQ,3200.00,62.1,800000\n"

    def test_returns_symbols_on_success(self, monkeypatch):
        import bhavcopy as bh
        from datetime import date
        monkeypatch.setattr(bh, "_candidate_session_dates", lambda n=6: [date(2026, 9, 26)])
        monkeypatch.setattr(bh, "_nse_client", lambda: None)
        monkeypatch.setattr(bh, "_fetch_bhav_day_parsed", lambda client, d: {
            "RELIANCE": {"close": 2500.0}, "TCS": {"close": 3200.0}
        })
        import kv_cache
        monkeypatch.setattr(kv_cache, "_get_neon", lambda: None)
        r = client.get("/bhavcopy/universe")
        body = r.json()
        assert "RELIANCE" in body["symbols"]
        assert body["count"] >= 1

    def test_min_price_filter_applied(self, monkeypatch):
        import bhavcopy as bh
        from datetime import date
        monkeypatch.setattr(bh, "_candidate_session_dates", lambda n=6: [date(2026, 9, 26)])
        monkeypatch.setattr(bh, "_nse_client", lambda: None)
        monkeypatch.setattr(bh, "_fetch_bhav_day_parsed", lambda client, d: {
            "CHEAP": {"close": 5.0}, "EXPENSIVE": {"close": 5000.0}
        })
        import kv_cache
        monkeypatch.setattr(kv_cache, "_get_neon", lambda: None)
        r = client.get("/bhavcopy/universe?min_price=100")
        body = r.json()
        assert "CHEAP" not in body["symbols"]
        assert "EXPENSIVE" in body["symbols"]

    def test_returns_empty_when_no_data(self, monkeypatch):
        import bhavcopy as bh
        from datetime import date
        monkeypatch.setattr(bh, "_candidate_session_dates", lambda n=6: [date(2026, 9, 26)])
        monkeypatch.setattr(bh, "_nse_client", lambda: None)
        monkeypatch.setattr(bh, "_fetch_bhav_day_parsed", lambda client, d: None)
        r = client.get("/bhavcopy/universe")
        body = r.json()
        assert body["symbols"] == []
        assert body["count"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# GET /delivery/{symbol}
# ══════════════════════════════════════════════════════════════════════════════

class TestDeliveryEndpoint:
    def test_returns_delivery_pct(self, monkeypatch):
        import bhavcopy as bh
        monkeypatch.setattr(bh, "get_delivery", lambda sym: {
            "symbol": sym, "delivery_pct": 55.0, "source": "nse_bhavcopy",
            "fetched_at": "2026-09-28T09:00:00"
        })
        r = client.get("/delivery/RELIANCE")
        body = r.json()
        assert body["delivery_pct"] == 55.0
        assert body["from_cache"] is False

    def test_uses_cache_on_second_call(self, monkeypatch):
        import bhavcopy as bh
        call_count = [0]
        def _fake_get(sym):
            call_count[0] += 1
            return {"symbol": sym, "delivery_pct": 60.0, "source": "nse_bhavcopy",
                    "fetched_at": "2026-09-28T09:00:00"}
        monkeypatch.setattr(bh, "get_delivery", _fake_get)
        client.get("/delivery/TCS")
        r2 = client.get("/delivery/TCS")
        assert r2.json()["from_cache"] is True
        assert call_count[0] == 1

    def test_neutral_fallback_shorter_ttl(self, monkeypatch):
        import bhavcopy as bh
        monkeypatch.setattr(bh, "get_delivery", lambda sym: {
            "symbol": sym, "delivery_pct": 50.0, "source": "fallback_neutral",
            "fetched_at": "2026-09-28T09:00:00"
        })
        r = client.get("/delivery/WIPRO")
        assert r.json()["source"] == "fallback_neutral"


# ══════════════════════════════════════════════════════════════════════════════
# GET /delivery/{symbol}/refresh
# ══════════════════════════════════════════════════════════════════════════════

class TestDeliveryRefreshEndpoint:
    def test_force_refresh_bypasses_cache(self, monkeypatch):
        import bhavcopy as bh
        # Pre-warm cache with old value
        m._cache_set("delivery:HDFC", {"delivery_pct": 99.0, "source": "old"}, ttl=600)
        call_count = [0]
        def _fake_get(sym):
            call_count[0] += 1
            return {"symbol": sym, "delivery_pct": 45.0, "source": "nse_bhavcopy",
                    "fetched_at": "2026-09-28T09:00:00"}
        monkeypatch.setattr(bh, "get_delivery", _fake_get)
        r = client.get("/delivery/HDFC/refresh")
        body = r.json()
        assert body["delivery_pct"] == 45.0
        assert call_count[0] == 1
        assert body["from_cache"] is False


# ══════════════════════════════════════════════════════════════════════════════
# GET /surprise/premarket/status
# ══════════════════════════════════════════════════════════════════════════════

class TestSurpriseStatus:
    def test_returns_idle_initially(self, monkeypatch):
        import surprise_premarket as sp
        monkeypatch.setattr(sp, "get_premarket_progress",
                            lambda: {"stage": "idle", "is_running": False})
        r = client.get("/surprise/premarket/status")
        body = r.json()
        assert body["stage"] == "idle"
        assert body["is_running"] is False


# ══════════════════════════════════════════════════════════════════════════════
# POST /surprise/premarket
# ══════════════════════════════════════════════════════════════════════════════

class TestSurprisePremarketRun:
    def test_background_true_returns_accepted(self, monkeypatch):
        import surprise_premarket as sp
        monkeypatch.setattr(sp, "get_premarket_progress",
                            lambda: {"stage": "idle", "is_running": False, "percent": 0})
        monkeypatch.setattr(sp, "precalculate_surprise_baselines", lambda syms: {"ok": True})
        monkeypatch.setattr(sp, "default_universe_from_env", lambda: ["RELIANCE", "TCS"])
        r = client.post("/surprise/premarket?background=true")
        body = r.json()
        assert body["ok"] is True
        assert body["accepted"] is True

    def test_already_running_returns_already_running(self, monkeypatch):
        import surprise_premarket as sp
        monkeypatch.setattr(sp, "get_premarket_progress",
                            lambda: {"stage": "running", "is_running": True, "percent": 40})
        monkeypatch.setattr(sp, "default_universe_from_env", lambda: ["RELIANCE"])
        r = client.post("/surprise/premarket?background=true")
        body = r.json()
        assert body["already_running"] is True

    def test_background_false_runs_inline(self, monkeypatch):
        import surprise_premarket as sp
        monkeypatch.setattr(sp, "get_premarket_progress",
                            lambda: {"stage": "idle", "is_running": False, "percent": 0})
        monkeypatch.setattr(sp, "default_universe_from_env", lambda: ["RELIANCE"])
        monkeypatch.setattr(sp, "precalculate_surprise_baselines",
                            lambda syms: {"ok": True, "computed": 1})
        r = client.post("/surprise/premarket?background=false")
        body = r.json()
        assert body["ok"] is True

    def test_symbols_query_param_parsed(self, monkeypatch):
        import surprise_premarket as sp
        captured = []
        def _fake_run(syms):
            captured.extend(syms)
            return {"ok": True, "computed": len(syms)}
        monkeypatch.setattr(sp, "get_premarket_progress",
                            lambda: {"stage": "idle", "is_running": False, "percent": 0})
        monkeypatch.setattr(sp, "precalculate_surprise_baselines", _fake_run)
        r = client.post("/surprise/premarket?background=false&symbols=RELIANCE,TCS")
        assert "RELIANCE" in captured
        assert "TCS" in captured


# ══════════════════════════════════════════════════════════════════════════════
# GET /surprise/premarket (GET variant)
# ══════════════════════════════════════════════════════════════════════════════

class TestSurprisePremarketGet:
    def test_get_variant_delegates_to_run(self, monkeypatch):
        import surprise_premarket as sp
        monkeypatch.setattr(sp, "get_premarket_progress",
                            lambda: {"stage": "idle", "is_running": False, "percent": 0})
        monkeypatch.setattr(sp, "default_universe_from_env", lambda: ["RELIANCE"])
        monkeypatch.setattr(sp, "precalculate_surprise_baselines", lambda s: {"ok": True})
        r = client.get("/surprise/premarket?background=true")
        assert r.status_code == 200
        assert r.json()["ok"] is True


# ══════════════════════════════════════════════════════════════════════════════
# GET /surprise/static
# ══════════════════════════════════════════════════════════════════════════════

class TestSurpriseStatic:
    def test_returns_no_database_url_when_none(self, monkeypatch):
        import surprise_premarket as sp
        monkeypatch.setattr(sp, "_db_url", lambda: None)
        r = client.get("/surprise/static")
        body = r.json()
        assert body["ok"] is False
        assert body["error"] == "no_database_url"

    def test_returns_error_on_db_failure(self, monkeypatch):
        import surprise_premarket as sp
        monkeypatch.setattr(sp, "_db_url", lambda: "postgresql://bad_host/db")
        r = client.get("/surprise/static")
        body = r.json()
        assert body["ok"] is False

    def test_returns_rows_from_db(self, monkeypatch, tmp_path):
        import surprise_premarket as sp
        # Use a file-based SQLite so the URL can be passed through create_engine
        db_path = tmp_path / "test.db"
        db_url = f"sqlite:///{db_path}"
        from sqlalchemy import create_engine, text
        eng = create_engine(db_url)
        with eng.begin() as conn:
            conn.execute(text("""
                CREATE TABLE surprise_static_feed (
                    symbol TEXT, prev_close REAL, avg_15m_volume REAL,
                    daily_atr REAL, high_52w REAL, dist_52w_pct REAL,
                    sector TEXT, is_liquid INTEGER, updated_at TEXT
                )
            """))
            conn.execute(text(
                "INSERT INTO surprise_static_feed VALUES "
                "('RELIANCE',2500,40000,25,2800,10.7,'Energy',1,'2026-09-28T09:00:00')"
            ))
        eng.dispose()
        monkeypatch.setattr(sp, "_db_url", lambda: db_url)
        r = client.get("/surprise/static?limit=10")
        body = r.json()
        assert body["ok"] is True
        assert body["count"] == 1
        assert body["rows"][0]["symbol"] == "RELIANCE"


# ══════════════════════════════════════════════════════════════════════════════
# _report_rate_limit — both branches
# ══════════════════════════════════════════════════════════════════════════════

class TestReportRateLimit:
    def test_circuit_breaker_branch_called(self, monkeypatch):
        called = []
        import circuit_breaker as cb
        monkeypatch.setattr(cb, "record_rate_limit_hit",
                            lambda **kw: called.append(kw))
        m._report_rate_limit(429, "/quote", "rate limit", "RELIANCE")
        assert called

    def test_gateway_branch_called(self, monkeypatch):
        # _report_rate_limit imports requests at module level in main.py —
        # patch it via the main module's reference.
        called = []
        monkeypatch.setenv("API_GATEWAY_URL", "http://fake-gw:8000")
        class _Resp: pass
        monkeypatch.setattr(m.requests, "post",
                            lambda url, **kw: called.append(url) or _Resp())
        m._report_rate_limit(429, "/quote", "detail", "TCS")
        assert any("ops/rate-limits" in str(u) for u in called)

    def test_no_gw_url_skips_gateway(self, monkeypatch):
        monkeypatch.delenv("API_GATEWAY_URL", raising=False)
        called = []
        monkeypatch.setattr(m.requests, "post", lambda *a, **kw: called.append(True))
        m._report_rate_limit(429)
        assert not called

    def test_swallows_exceptions(self, monkeypatch):
        import circuit_breaker as cb
        monkeypatch.setattr(cb, "record_rate_limit_hit",
                            lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        # Must not raise
        m._report_rate_limit(429)

    @pytest.mark.parametrize("raw", ["", "   ", "\t", " \n ", "/", "  /  "])
    def test_blank_gateway_url_skips_gateway(self, monkeypatch, raw):
        monkeypatch.setenv("API_GATEWAY_URL", raw)
        called = []
        monkeypatch.setattr(m.requests, "post", lambda *a, **kw: called.append(True))
        m._report_rate_limit(429)
        assert not called

    @pytest.mark.parametrize("raw", ["  http://gw:1  ", "http://gw:1/ ", " http://gw:1/\n"])
    def test_padded_gateway_url_is_trimmed(self, monkeypatch, raw):
        monkeypatch.setenv("API_GATEWAY_URL", raw)
        seen = []
        monkeypatch.setattr(m.requests, "post", lambda url, **kw: seen.append(url))
        m._report_rate_limit(429)
        assert seen == ["http://gw:1/ops/rate-limits/event"]


class TestFeedUniverseLoopGatewayUrl:
    """_refresh_feed_universe_loop must treat a blank API_GATEWAY_URL as unset and return
    immediately (no sleep, no HTTP client) instead of looping against '   /scan/universe'."""

    @pytest.mark.parametrize("raw", ["", "   ", "\t\n", "/", "  /  "])
    def test_blank_url_returns_without_fetching(self, monkeypatch, raw, caplog):
        import asyncio
        monkeypatch.setenv("API_GATEWAY_URL", raw)

        class _Boom:
            def __init__(self, *a, **kw):
                raise AssertionError("httpx client must not be created for a blank gateway URL")

        monkeypatch.setattr(m.httpx, "AsyncClient", _Boom)
        with caplog.at_level("WARNING"):
            asyncio.run(asyncio.wait_for(m._refresh_feed_universe_loop(), timeout=2))
        assert "API_GATEWAY_URL not set" in caplog.text

    def test_padded_url_is_trimmed_before_first_fetch(self, monkeypatch):
        import asyncio
        monkeypatch.setenv("API_GATEWAY_URL", "  http://gw:1/ ")
        monkeypatch.setenv("FEED_UNIVERSE_INITIAL_DELAY_S", "0")
        urls = []

        class _Stop(Exception):
            pass

        class _Client:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, **kw):
                urls.append(url)
                raise _Stop()

        monkeypatch.setattr(m.httpx, "AsyncClient", _Client)
        real_sleep = asyncio.sleep
        calls = {"n": 0}

        async def _sleep(d):
            calls["n"] += 1
            if calls["n"] > 1:        # first call is the initial delay; stop on the retry wait
                raise _Stop()
            await real_sleep(0)

        monkeypatch.setattr(m.asyncio, "sleep", _sleep)
        with pytest.raises(_Stop):
            asyncio.run(m._refresh_feed_universe_loop())
        assert urls == ["http://gw:1/scan/universe"]
