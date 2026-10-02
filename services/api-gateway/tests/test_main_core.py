"""tests/test_main_core.py — coverage for api-gateway/main.py, slice 1 (lines 1-800)

Pass 59 is the first of several passes over the 11,888-line `main.py`. This slice is the
module bootstrap and everything a route group depends on:

* env-driven service URLs / cache constants and the optional-import fallbacks;
* the USE_REDIS / DISABLE_REDIS switch (Upstash connect, ping failure, missing credentials);
* the shared httpx client, startup / shutdown hooks, CORS middleware, catch-all handler;
* the global activity gate (`set_activity_paused`) and `_graceful_shutdown_commit`;
* `_feed_store`, `_redis_get` / `_redis_set` / `_redis_soft_ttl_refresh` (memory + kv_cache +
  optional Redis), the watchlist / searched-symbol helpers;
* the NSE session (`_get_nse_client`) and `_fetch_from_nse_api`.

No network, no real KV / DB / Redis: every collaborator is a small fake installed with
`monkeypatch`. `main.py` is imported normally for the bulk of the tests; tests that need the
module-level code to run again under different env / sys.modules (Redis switch, optional
imports, URL derivation) execute a *fresh copy* of the file under another module name
(`_load_probe`) so the canonical `main` module and its globals are never disturbed.
Nothing sleeps; no test starts a server.

Findings are pinned as current behaviour and marked ``NOT FIXED``; the ones fixed afterwards (notification
URL trailing slash, scan statuses invisible to shutdown / stop-all under kv_cache, `_add_searched` whitespace)
now pin the fixed behaviour.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_core.py -v
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import types

import pytest

# `import main` executes the module-level Redis switch. conftest only scrubs the environment per
# test (after collection), so scrub it here too: a developer shell that exports USE_REDIS=1 with
# real Upstash credentials must never make *collecting* this file ping a live Redis.
_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)

_MAIN_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")


# ── helpers / fakes ──────────────────────────────────────────────────────────

def _fake_rate_limiter(raises=None):
    """A stand-in `rate_limiter` so re-executing main.py never re-patches real yfinance."""
    m = types.ModuleType("rate_limiter")
    m.calls = []

    def patch_yfinance():
        m.calls.append(1)
        if raises is not None:
            raise raises
        return True

    m.patch_yfinance = patch_yfinance
    return m


def _load_probe(monkeypatch, tmp_path, env=None, absent=(), rate_limiter=None):
    """Execute a fresh copy of main.py under a throw-away module name.

    `env` is applied before the run; every module in `absent` is made un-importable
    (sys.modules[name] = None makes `import name` raise ImportError).
    """
    monkeypatch.setenv("YF_TZ_CACHE_DIR", str(tmp_path / "yf_tz"))
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    for name in absent:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "rate_limiter", rate_limiter or _fake_rate_limiter())
    yf = types.ModuleType("yfinance")
    yf.tz_cache_calls = []
    yf.set_tz_cache_location = yf.tz_cache_calls.append
    monkeypatch.setitem(sys.modules, "yfinance", yf)
    spec = importlib.util.spec_from_file_location("main_probe_core", _MAIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._probe_yf = yf
    return mod


class FakeUpstash:
    """upstash_redis module whose Redis class records construction and can fail its ping."""

    def __init__(self, ping_raises=None):
        self.instances = []
        outer = self

        class Redis:
            def __init__(self, url=None, token=None):
                self.url, self.token = url, token
                self.pinged = False
                outer.instances.append(self)

            def ping(self):
                self.pinged = True
                if ping_raises is not None:
                    raise ping_raises

        self.module = types.ModuleType("upstash_redis")
        self.module.Redis = Redis


class FakeRedisClient:
    """Minimal Upstash-like client used for the `_redis` global."""

    def __init__(self):
        self.data = {}
        self.ttl_value = -1
        self.calls = []
        self.get_raises = None
        self.ttl_raises = None
        self.write_raises = None

    def get(self, key):
        if self.get_raises is not None:
            raise self.get_raises
        return self.data.get(key)

    def ttl(self, key):
        if self.ttl_raises is not None:
            raise self.ttl_raises
        return self.ttl_value

    def setex(self, key, ttl, payload):
        if self.write_raises is not None:
            raise self.write_raises
        self.calls.append(("setex", key, ttl, payload))

    def set(self, key, payload):
        if self.write_raises is not None:
            raise self.write_raises
        self.calls.append(("set", key, payload))


class FakeKVCache:
    """kv_cache module double for `_redis_get` / `_redis_set` / watchlist helpers."""

    def __init__(self):
        self.store = {}
        self.set_calls = []
        self.get_raises = None
        self.set_raises = None
        self.watchlist_value = None
        self.watchlist_get_raises = None
        self.watchlist_set_raises = None
        self.watchlist_sets = []

    def get(self, key):
        if self.get_raises is not None:
            raise self.get_raises
        return self.store.get(key)

    def set(self, key, value, ttl=None):
        if self.set_raises is not None:
            raise self.set_raises
        self.set_calls.append((key, value, ttl))
        self.store[key] = value

    def watchlist_get(self):
        if self.watchlist_get_raises is not None:
            raise self.watchlist_get_raises
        return self.watchlist_value

    def watchlist_set(self, symbols):
        if self.watchlist_set_raises is not None:
            raise self.watchlist_set_raises
        self.watchlist_sets.append(list(symbols))


class RecLogger:
    def __init__(self):
        self.infos = []

    def info(self, msg, *a, **k):
        self.infos.append(msg % a if a else msg)

    def warning(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


class Clock:
    """Stands in for main._time_mod / main.time."""

    def __init__(self, now=1_000_000.0):
        self.now = now

    def time(self):
        return self.now


class RecJSONResponse:
    def __init__(self, content=None, status_code=200, headers=None):
        self.content, self.status_code, self.headers = content, status_code, headers


def _run(coro):
    return asyncio.run(coro)


# ── module-level configuration ───────────────────────────────────────────────

def test_service_urls_default_when_env_absent(monkeypatch, tmp_path):
    for k in ("DECISION_PREDICTION_URL", "ANALYSIS_INTELLIGENCE_URL", "NOTIFICATION_SCHEDULER_URL",
              "DECISION_URL", "NOTIFICATION_URL", "NEWS_URL", "MARKET_DATA_URL", "TECHNICAL_URL",
              "FUNDAMENTAL_URL", "SCHEDULER_URL", "EVENT_URL", "PREDICTION_URL",
              "MARKET_SENTIMENT_URL", "TRAINING_URL"):
        monkeypatch.delenv(k, raising=False)
    m = _load_probe(monkeypatch, tmp_path)
    assert m.DECISION_URL == "https://decision-prediction-service.onrender.com/decision"
    assert m.PREDICTION_URL == "https://decision-prediction-service.onrender.com/prediction"
    assert m.TRAINING_URL == "https://decision-prediction-service.onrender.com/training"
    assert m.NEWS_URL == "https://analysis-intelligence-service.onrender.com/news"
    assert m.TECHNICAL_URL.endswith("/technical")
    assert m.FUNDAMENTAL_URL.endswith("/fundamental")
    assert m.EVENT_URL.endswith("/event")
    assert m.MARKET_SENTIMENT_URL.endswith("/sentiment")
    assert m.NOTIFICATION_URL == "https://notification-scheduler-service-x8vc.onrender.com/notification"
    # SCHEDULER_URL is the same host, different sub-app mount, without the /notification suffix
    assert m.SCHEDULER_URL == "https://notification-scheduler-service-x8vc.onrender.com/scheduler"
    assert m.MARKET_DATA_URL == "https://market-data-service-r6d7.onrender.com"


def test_service_urls_derive_from_base_env(monkeypatch, tmp_path):
    for k in ("DECISION_URL", "NOTIFICATION_URL", "NEWS_URL", "SCHEDULER_URL", "PREDICTION_URL"):
        monkeypatch.delenv(k, raising=False)
    m = _load_probe(monkeypatch, tmp_path, env={
        "DECISION_PREDICTION_URL": "https://dp.example/",          # trailing slash is stripped
        "ANALYSIS_INTELLIGENCE_URL": "https://ai.example",
        "NOTIFICATION_SCHEDULER_URL": "https://ns.example",        # bare host: /notification appended
    })
    assert m.DECISION_URL == "https://dp.example/decision"
    assert m.PREDICTION_URL == "https://dp.example/prediction"
    assert m.NEWS_URL == "https://ai.example/news"
    assert m.NOTIFICATION_URL == "https://ns.example/notification"
    assert m.SCHEDULER_URL == "https://ns.example/scheduler"


def test_notification_url_kept_when_base_already_ends_in_notification(monkeypatch, tmp_path):
    monkeypatch.delenv("NOTIFICATION_URL", raising=False)
    monkeypatch.delenv("SCHEDULER_URL", raising=False)
    m = _load_probe(monkeypatch, tmp_path, env={"NOTIFICATION_SCHEDULER_URL": "https://ns.example/notification"})
    assert m.NOTIFICATION_URL == "https://ns.example/notification"
    assert m.SCHEDULER_URL == "https://ns.example/scheduler"


def test_notification_url_trailing_slash_of_the_base_is_stripped(monkeypatch, tmp_path):
    """FIXED: when NOTIFICATION_SCHEDULER_URL already ended in "/notification/" the value used to be taken
    verbatim, trailing slash included, so f"{NOTIFICATION_URL}/notify" sent "//notify". It is now
    rstrip('/')-ed like every other derived URL."""
    monkeypatch.delenv("NOTIFICATION_URL", raising=False)
    monkeypatch.delenv("SCHEDULER_URL", raising=False)
    m = _load_probe(monkeypatch, tmp_path, env={"NOTIFICATION_SCHEDULER_URL": "https://ns.example/notification/"})
    assert m.NOTIFICATION_URL == "https://ns.example/notification"
    assert m.SCHEDULER_URL == "https://ns.example/scheduler"
    m = _load_probe(monkeypatch, tmp_path, env={"NOTIFICATION_SCHEDULER_URL": "https://ns.example/notification///"})
    assert m.NOTIFICATION_URL == "https://ns.example/notification"


def test_explicit_url_env_overrides_win(monkeypatch, tmp_path):
    m = _load_probe(monkeypatch, tmp_path, env={
        "DECISION_URL": "http://d/x", "NEWS_URL": "http://n/x", "SCHEDULER_URL": "http://s/x",
        "MARKET_DATA_URL": "http://m/x", "EVENT_URL": "http://e/x",
    })
    assert (m.DECISION_URL, m.NEWS_URL, m.SCHEDULER_URL) == ("http://d/x", "http://n/x", "http://s/x")
    assert (m.MARKET_DATA_URL, m.EVENT_URL) == ("http://m/x", "http://e/x")


def test_system_services_table_shape():
    svc = gw.SYSTEM_SERVICES
    assert set(svc) == {
        "market-data", "technical-analysis", "fundamental-analysis", "decision-engine",
        "news-intelligence", "event-tracker", "prediction", "notification",
        "market-sentiment", "training",
    }
    required = {k for k, v in svc.items() if v["required"]}
    assert required == {"market-data", "technical-analysis", "fundamental-analysis", "decision-engine"}
    assert svc["market-data"]["url"] == gw.MARKET_DATA_URL
    assert svc["decision-engine"]["url"] == gw.DECISION_URL
    assert svc["training"]["url"] == gw.TRAINING_URL


def test_env_driven_cache_constants_parse(monkeypatch, tmp_path):
    m = _load_probe(monkeypatch, tmp_path, env={
        "STATIC_PARAM_TTL": "5", "LAST_FULL_SCAN_TTL": "6", "DECIDE_CACHE_TTL_OPEN": "7",
        "DECIDE_CACHE_TTL_CLOSED": "8", "BATCH_RESULT_CACHE": "0", "SCAN_LITE_DEFAULT": "Yes",
        "WAKE_BEFORE_SCAN": "false", "WAKE_WAIT_SECONDS": "3.5",
    })
    assert (m.STATIC_PARAM_TTL, m.LAST_FULL_SCAN_TTL) == (5, 6)
    assert (m.DECIDE_CACHE_TTL_OPEN, m.DECIDE_CACHE_TTL_CLOSED) == (7, 8)
    assert m.BATCH_RESULT_CACHE_ENABLED is False
    assert m.SCAN_LITE_DEFAULT is True            # "Yes" is case-insensitive
    assert m.WAKE_BEFORE_SCAN is False
    assert m.WAKE_WAIT_SECONDS == 3.5


def test_env_driven_cache_constants_defaults(monkeypatch, tmp_path):
    for k in ("STATIC_PARAM_TTL", "LAST_FULL_SCAN_TTL", "DECIDE_CACHE_TTL_OPEN", "DECIDE_CACHE_TTL_CLOSED",
              "BATCH_RESULT_CACHE", "SCAN_LITE_DEFAULT", "WAKE_BEFORE_SCAN", "WAKE_WAIT_SECONDS"):
        monkeypatch.delenv(k, raising=False)
    m = _load_probe(monkeypatch, tmp_path)
    assert m.STATIC_PARAM_TTL == 86400 and m.LAST_FULL_SCAN_TTL == 86400
    assert m.DECIDE_CACHE_TTL_OPEN == 300 and m.DECIDE_CACHE_TTL_CLOSED == 21600
    assert m.BATCH_RESULT_CACHE_ENABLED is True
    assert m.SCAN_LITE_DEFAULT is False
    assert m.WAKE_BEFORE_SCAN is True and m.WAKE_WAIT_SECONDS == 12.0


def test_kv_key_constants_are_namespaced_and_distinct():
    keys = [gw.WATCHLIST_KEY, gw.SEARCHED_KEY, gw.SCAN_UNIVERSE_KEY, gw.SCAN_UNIVERSE_STALE_KEY,
            gw.IPO_CACHE_KEY, gw.KNOWN_SYMBOLS_KEY, gw.SCAN_TASK_PREFIX, gw.MOMENTUM_MOVERS_CACHE_KEY,
            gw.MARKET_MOVERS_CACHE_PREFIX, gw.INDICES_CACHE_KEY, gw.INDICES_LAST_KNOWN,
            gw.FUNDAMENTAL_CACHE_PREFIX, gw.EVENT_CACHE_PREFIX, gw.NEWS_CACHE_PREFIX,
            gw.LAST_FULL_SCAN_KEY, gw.DECIDE_CACHE_PREFIX, gw.BATCH_RESULT_CACHE_PREFIX]
    assert all(k.startswith("stockky:") for k in keys)
    assert len(set(keys)) == len(keys)
    # the stale fallback must live under the durable "stockky:scan_universe" prefix (see main.py comment)
    assert gw.SCAN_UNIVERSE_STALE_KEY.startswith(gw.SCAN_UNIVERSE_KEY)
    assert gw.MOMENTUM_MOVERS_CACHE_TTL == 90


def test_symbol_alias_table_is_consistent_with_extra_new_symbols():
    for target in gw.SYMBOL_ALIASES.values():
        assert target in gw.EXTRA_NEW_SYMBOLS
    assert gw.SYMBOL_ALIASES["TATAMOTORS"] == "TMPV"
    assert gw.SYMBOL_ALIASES["ZOMATO"] == "ETERNAL"


def test_derivative_contract_regex():
    rx = gw._DERIVATIVE_CONTRACT_RE
    assert rx.search("APLAPOLLO29SEP26FUT")
    assert rx.search("BANKNIFTY29SEP2648000CE")
    assert rx.search("NIFTY29SEP2612.5PE")
    assert not rx.search("RELIANCE")
    assert not rx.search("BAJFINANCE")
    assert not rx.search("FUTURA")              # must be date + FUT at the END


def test_symbol_hygiene_tables():
    assert "NIFTY50" in gw._INDEX_PSEUDO_TOKENS and "INDIAVIX" in gw._INDEX_PSEUDO_TOKENS
    assert gw._DELISTED_RENAME["MOTHERSUMI"] == "MOTHERSON"
    assert gw._DELISTED_RENAME["IBULHSGFIN"] is None
    assert gw._NSE_CLIENT_BOOTSTRAP_HEADERS["Sec-Fetch-Mode"] == "navigate"
    assert gw._NSE_CLIENT_BOOTSTRAP_HEADERS["User-Agent"] == gw._NSE_CLIENT_HEADERS["User-Agent"]
    assert "json" in gw._NSE_CLIENT_HEADERS["Accept"]


# ── optional imports / import-time fallbacks ─────────────────────────────────

def test_optional_imports_degrade_to_fallbacks(monkeypatch, tmp_path):
    m = _load_probe(monkeypatch, tmp_path, absent=("kv_cache", "qstash_client", "json_safe"))
    assert m._kv_cache is None
    assert m.qstash_client is None
    payload = {"a": float("nan")}
    assert m._json_sanitize(payload) is payload           # identity fallback


def test_optional_imports_present_use_real_modules(monkeypatch, tmp_path):
    fake_json_safe = types.ModuleType("json_safe")
    fake_json_safe.sanitize = lambda x: {"sanitised": x}
    fake_qstash = types.ModuleType("qstash_client")
    fake_kv = types.ModuleType("kv_cache")
    monkeypatch.setitem(sys.modules, "json_safe", fake_json_safe)
    monkeypatch.setitem(sys.modules, "qstash_client", fake_qstash)
    monkeypatch.setitem(sys.modules, "kv_cache", fake_kv)
    m = _load_probe(monkeypatch, tmp_path)
    assert m._kv_cache is fake_kv
    assert m.qstash_client is fake_qstash
    assert m._json_sanitize(1) == {"sanitised": 1}


def test_safe_json_response_sanitises_content(monkeypatch):
    import fastapi.responses as fr
    monkeypatch.setattr(fr, "JSONResponse", RecJSONResponse)
    monkeypatch.setattr(gw, "_json_sanitize", lambda x: {"clean": x})
    r = gw._safe_json_response({"v": 1}, status_code=207)
    assert isinstance(r, RecJSONResponse)
    assert r.content == {"clean": {"v": 1}} and r.status_code == 207
    assert gw._safe_json_response([]).status_code == 200


def test_rate_limiter_patch_runs_once_at_import(monkeypatch, tmp_path):
    rl = _fake_rate_limiter()
    _load_probe(monkeypatch, tmp_path, rate_limiter=rl)
    assert rl.calls == [1]


def test_rate_limiter_patch_failure_is_swallowed(monkeypatch, tmp_path):
    rl = _fake_rate_limiter(raises=RuntimeError("boom"))
    m = _load_probe(monkeypatch, tmp_path, rate_limiter=rl)
    assert rl.calls == [1]
    assert m.app is not None                                # module still finished loading


def test_yfinance_tz_cache_directory_is_created(monkeypatch, tmp_path):
    target = tmp_path / "custom_tz"
    assert not target.exists()
    m = _load_probe(monkeypatch, tmp_path, env={"YF_TZ_CACHE_DIR": str(target)})
    assert target.is_dir()
    assert m._probe_yf.tz_cache_calls == [str(target)]


# ── Redis switch ─────────────────────────────────────────────────────────────

def test_redis_is_off_by_default(monkeypatch, tmp_path):
    up = FakeUpstash()
    monkeypatch.setitem(sys.modules, "upstash_redis", up.module)
    m = _load_probe(monkeypatch, tmp_path)
    assert m._USE_REDIS is False and m._redis is None
    assert up.instances == []                               # never even constructed


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes"])
def test_use_redis_connects_when_credentials_present(monkeypatch, tmp_path, truthy):
    up = FakeUpstash()
    monkeypatch.setitem(sys.modules, "upstash_redis", up.module)
    m = _load_probe(monkeypatch, tmp_path, env={
        "USE_REDIS": truthy, "UPSTASH_REDIS_REST_URL": "https://u.example", "UPSTASH_REDIS_REST_TOKEN": "tok"})
    assert m._USE_REDIS is True
    assert len(up.instances) == 1
    inst = up.instances[0]
    assert (inst.url, inst.token, inst.pinged) == ("https://u.example", "tok", True)
    assert m._redis is inst


def test_use_redis_ping_failure_falls_back_to_none(monkeypatch, tmp_path):
    up = FakeUpstash(ping_raises=ConnectionError("no route"))
    monkeypatch.setitem(sys.modules, "upstash_redis", up.module)
    m = _load_probe(monkeypatch, tmp_path, env={
        "USE_REDIS": "1", "UPSTASH_REDIS_REST_URL": "https://u.example", "UPSTASH_REDIS_REST_TOKEN": "tok"})
    assert m._USE_REDIS is True and m._redis is None
    assert up.instances[0].pinged is True


def test_use_redis_import_failure_falls_back_to_none(monkeypatch, tmp_path):
    m = _load_probe(monkeypatch, tmp_path, absent=("upstash_redis",), env={
        "USE_REDIS": "1", "UPSTASH_REDIS_REST_URL": "https://u.example", "UPSTASH_REDIS_REST_TOKEN": "tok"})
    assert m._redis is None


@pytest.mark.parametrize("missing", ["url", "token", "both"])
def test_use_redis_without_credentials_stays_off(monkeypatch, tmp_path, missing):
    up = FakeUpstash()
    monkeypatch.setitem(sys.modules, "upstash_redis", up.module)
    env = {"USE_REDIS": "1"}
    if missing == "token":
        env["UPSTASH_REDIS_REST_URL"] = "https://u.example"
    if missing == "url":
        env["UPSTASH_REDIS_REST_TOKEN"] = "tok"
    m = _load_probe(monkeypatch, tmp_path, env=env)
    assert m._redis is None and up.instances == []


@pytest.mark.parametrize("kill_switch", [("DISABLE_REDIS", "1"), ("DISABLE_REDIS", "true"),
                                         ("DISABLE_UPSTASH", "yes"), ("DISABLE_UPSTASH", "1")])
def test_disable_switches_override_use_redis(monkeypatch, tmp_path, kill_switch):
    up = FakeUpstash()
    monkeypatch.setitem(sys.modules, "upstash_redis", up.module)
    m = _load_probe(monkeypatch, tmp_path, env={
        "USE_REDIS": "1", "UPSTASH_REDIS_REST_URL": "https://u.example",
        "UPSTASH_REDIS_REST_TOKEN": "tok", kill_switch[0]: kill_switch[1]})
    assert m._USE_REDIS is False and m._redis is None and up.instances == []


# ── shared http client ───────────────────────────────────────────────────────

class FakeAsyncClient:
    instances = []

    def __init__(self, **kw):
        self.kw = kw
        self.is_closed = False
        self.aclose_calls = 0
        self.aclose_raises = None
        FakeAsyncClient.instances.append(self)

    async def aclose(self):
        self.aclose_calls += 1
        if self.aclose_raises is not None:
            raise self.aclose_raises
        self.is_closed = True


@pytest.fixture
def fake_async_client(monkeypatch):
    FakeAsyncClient.instances = []
    monkeypatch.setattr(gw.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(gw, "_shared_http_client", None)
    return FakeAsyncClient


def test_get_http_client_lazily_creates_with_pool_settings(fake_async_client):
    c = gw._get_http_client()
    assert c is fake_async_client.instances[0]
    assert c.kw["limits"] is gw._HTTP_LIMITS
    assert c.kw["timeout"] is gw._HTTP_TIMEOUT
    assert c.kw["follow_redirects"] is True
    assert gw._shared_http_client is c


def test_get_http_client_reuses_open_client(fake_async_client):
    first = gw._get_http_client()
    assert gw._get_http_client() is first
    assert len(fake_async_client.instances) == 1


def test_get_http_client_replaces_closed_client(fake_async_client):
    first = gw._get_http_client()
    first.is_closed = True
    second = gw._get_http_client()
    assert second is not first and len(fake_async_client.instances) == 2


# ── lifecycle hooks ──────────────────────────────────────────────────────────

def _fake_boot_forensics(monkeypatch, record_raises=None, clean_raises=None):
    m = types.ModuleType("boot_forensics")
    m.recorded, m.clean = [], []

    def record_boot(name):
        m.recorded.append(name)
        if record_raises is not None:
            raise record_raises

    def mark_clean_shutdown():
        m.clean.append(True)
        if clean_raises is not None:
            raise clean_raises

    m.record_boot, m.mark_clean_shutdown = record_boot, mark_clean_shutdown
    monkeypatch.setitem(sys.modules, "boot_forensics", m)
    return m


def test_boot_forensics_startup_records_service_name(monkeypatch):
    bf = _fake_boot_forensics(monkeypatch)
    _run(gw._boot_forensics_startup())
    assert bf.recorded == ["api-gateway"]


def test_boot_forensics_startup_swallows_errors(monkeypatch):
    bf = _fake_boot_forensics(monkeypatch, record_raises=RuntimeError("disk full"))
    _run(gw._boot_forensics_startup())
    assert bf.recorded == ["api-gateway"]
    monkeypatch.setitem(sys.modules, "boot_forensics", None)          # module unavailable
    _run(gw._boot_forensics_startup())


def test_boot_forensics_shutdown_marks_clean_and_swallows_errors(monkeypatch):
    bf = _fake_boot_forensics(monkeypatch)
    _run(gw._boot_forensics_shutdown())
    assert bf.clean == [True]
    bf2 = _fake_boot_forensics(monkeypatch, clean_raises=RuntimeError("x"))
    _run(gw._boot_forensics_shutdown())
    assert bf2.clean == [True]
    monkeypatch.setitem(sys.modules, "boot_forensics", None)
    _run(gw._boot_forensics_shutdown())


def test_lifecycle_hooks_are_registered_on_the_app():
    startup = {f.__name__ for f in gw.app.router.on_startup}
    shutdown = {f.__name__ for f in gw.app.router.on_shutdown}
    assert {"_boot_forensics_startup", "_start_shared_http"} <= startup
    assert {"_boot_forensics_shutdown", "_graceful_shutdown"} <= shutdown


class _StartupFakes:
    def __init__(self, monkeypatch):
        self.limiter_calls, self.heal_calls, self.reset_calls = [], [], []
        self.limiter_raises = self.heal_raises = self.reset_raises = None
        outer = self

        class Limiter:
            def set_redis(self, client):
                outer.limiter_calls.append(client)
                if outer.limiter_raises is not None:
                    raise outer.limiter_raises

        monkeypatch.setattr(gw, "redis_limiter", Limiter())

        import data_feed
        import circuit_breaker

        def heal():
            outer.heal_calls.append(1)
            if outer.heal_raises is not None:
                raise outer.heal_raises
            return {"cleared": True}

        def reset():
            outer.reset_calls.append(1)
            if outer.reset_raises is not None:
                raise outer.reset_raises
            return ["market-data", "decision"]

        monkeypatch.setattr(data_feed, "clear_stuck_feed_job_on_boot", heal)
        monkeypatch.setattr(circuit_breaker, "reset_all_breakers", reset)


def test_startup_creates_client_and_runs_every_boot_step(monkeypatch, fake_async_client):
    fk = _StartupFakes(monkeypatch)
    sentinel = object()
    monkeypatch.setattr(gw, "_redis", sentinel)
    _run(gw._start_shared_http())
    assert len(fake_async_client.instances) == 1 and gw._shared_http_client is fake_async_client.instances[0]
    assert fk.limiter_calls == [sentinel]
    assert fk.heal_calls == [1] and fk.reset_calls == [1]


def test_startup_keeps_existing_open_client(monkeypatch, fake_async_client):
    _StartupFakes(monkeypatch)
    existing = FakeAsyncClient()
    fake_async_client.instances = []
    monkeypatch.setattr(gw, "_shared_http_client", existing)
    _run(gw._start_shared_http())
    assert gw._shared_http_client is existing and fake_async_client.instances == []


def test_startup_replaces_closed_client(monkeypatch, fake_async_client):
    _StartupFakes(monkeypatch)
    dead = FakeAsyncClient()
    dead.is_closed = True
    fake_async_client.instances = []
    monkeypatch.setattr(gw, "_shared_http_client", dead)
    _run(gw._start_shared_http())
    assert gw._shared_http_client is not dead and len(fake_async_client.instances) == 1


def test_startup_is_non_fatal_and_every_step_still_runs_when_each_step_fails(monkeypatch):
    fk = _StartupFakes(monkeypatch)

    class Boom:
        def __init__(self, **kw):
            raise RuntimeError("cannot build client")

    monkeypatch.setattr(gw.httpx, "AsyncClient", Boom)
    monkeypatch.setattr(gw, "_shared_http_client", None)
    fk.limiter_raises = RuntimeError("limiter")
    fk.heal_raises = RuntimeError("neon down")
    fk.reset_raises = RuntimeError("breakers")
    _run(gw._start_shared_http())                       # must not raise
    assert fk.limiter_calls and fk.heal_calls == [1] and fk.reset_calls == [1]
    assert gw._shared_http_client is None


def _shutdown_setup(monkeypatch, commit_raises=None):
    calls = []

    def commit(reason="shutdown"):
        calls.append(reason)
        if commit_raises is not None:
            raise commit_raises
        return [{"phase": "x", "ok": True}]

    monkeypatch.setattr(gw, "_graceful_shutdown_commit", commit)
    return calls


def test_graceful_shutdown_commits_cancels_quote_loop_and_closes_client(monkeypatch):
    calls = _shutdown_setup(monkeypatch)
    client = FakeAsyncClient()

    async def scenario():
        task = asyncio.ensure_future(asyncio.sleep(60))
        monkeypatch.setattr(gw, "_quote_loop_task", task)
        monkeypatch.setattr(gw, "_shared_http_client", client)
        await gw._graceful_shutdown()
        return task

    task = _run(scenario())
    assert calls == ["process_shutdown"]
    assert task.cancelled()
    assert gw._quote_loop_task is None
    assert client.aclose_calls == 1 and gw._shared_http_client is None


def test_graceful_shutdown_with_nothing_to_close(monkeypatch):
    calls = _shutdown_setup(monkeypatch)
    monkeypatch.setattr(gw, "_quote_loop_task", None)
    monkeypatch.setattr(gw, "_shared_http_client", None)
    _run(gw._graceful_shutdown())
    assert calls == ["process_shutdown"]


def test_graceful_shutdown_skips_finished_task_and_closed_client(monkeypatch):
    _shutdown_setup(monkeypatch)
    closed = FakeAsyncClient()
    closed.is_closed = True

    async def scenario():
        done = asyncio.ensure_future(asyncio.sleep(0))
        await done
        monkeypatch.setattr(gw, "_quote_loop_task", done)
        monkeypatch.setattr(gw, "_shared_http_client", closed)
        await gw._graceful_shutdown()
        return done

    done = _run(scenario())
    assert closed.aclose_calls == 0
    assert gw._quote_loop_task is done                # untouched: only cancelled tasks are cleared


def test_graceful_shutdown_survives_every_failure(monkeypatch):
    _shutdown_setup(monkeypatch, commit_raises=RuntimeError("commit blew up"))

    class BadTask:
        def done(self):
            raise RuntimeError("done() blew up")

    client = FakeAsyncClient()
    client.aclose_raises = RuntimeError("close blew up")
    monkeypatch.setattr(gw, "_quote_loop_task", BadTask())
    monkeypatch.setattr(gw, "_shared_http_client", client)
    _run(gw._graceful_shutdown())                      # must not raise
    assert client.aclose_calls == 1


def test_graceful_shutdown_tolerates_task_that_raises_when_awaited(monkeypatch):
    _shutdown_setup(monkeypatch)

    async def scenario():
        async def failing():
            await asyncio.sleep(60)

        task = asyncio.ensure_future(failing())
        await asyncio.sleep(0)

        class Wrapper:
            """awaitable whose cancel() works but whose await raises a non-Cancelled error"""
            def done(self):
                return False

            def cancel(self):
                task.cancel()

            def __await__(self):
                raise RuntimeError("weird await failure")
                yield  # pragma: no cover

        monkeypatch.setattr(gw, "_quote_loop_task", Wrapper())
        monkeypatch.setattr(gw, "_shared_http_client", None)
        await gw._graceful_shutdown()

    _run(scenario())
    assert gw._quote_loop_task is None


# ── middleware / catch-all handler ───────────────────────────────────────────

def test_cors_middleware_stamps_wildcard_headers():
    class Resp:
        def __init__(self):
            self.headers = {}

    resp = Resp()

    async def call_next(request):
        assert request == "REQ"
        return resp

    out = _run(gw.add_cors_header("REQ", call_next))
    assert out is resp
    assert resp.headers == {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "*",
        "Access-Control-Allow-Headers": "*",
    }


def test_universal_exception_handler_returns_500_with_cors(monkeypatch):
    monkeypatch.setattr(gw, "JSONResponse", RecJSONResponse)
    r = _run(gw.universal_exception_handler(None, ValueError("kaboom")))
    assert r.status_code == 500
    assert r.content == {"detail": "Internal server error: kaboom"}
    assert r.headers["Access-Control-Allow-Origin"] == "*"
    assert r.headers["Access-Control-Allow-Methods"] == "*"
    assert r.headers["Access-Control-Allow-Headers"] == "*"


# ── activity gate ────────────────────────────────────────────────────────────

@pytest.fixture
def gate(monkeypatch):
    rec = types.SimpleNamespace(stop=[], clear=[], stop_raises=None, clear_raises=None)

    def stop():
        rec.stop.append(1)
        if rec.stop_raises is not None:
            raise rec.stop_raises

    def clear():
        rec.clear.append(1)
        if rec.clear_raises is not None:
            raise rec.clear_raises

    monkeypatch.setattr(gw, "request_data_feed_stop", stop)
    monkeypatch.setattr(gw, "clear_data_feed_stop", clear)
    monkeypatch.setattr(gw, "_ACTIVITY_PAUSED", False)
    monkeypatch.setattr(gw, "_QUOTE_LOOP_ENABLED", True)
    monkeypatch.setattr(gw, "_SCAN_IN_PROGRESS", False)
    return rec


def test_activity_gate_pause_stops_feed_and_quote_loop(gate):
    gw.set_activity_paused(True)
    assert gw.activity_paused() is True
    assert gw._QUOTE_LOOP_ENABLED is False
    assert gate.stop == [1] and gate.clear == []


def test_activity_gate_resume_clears_stop_and_reenables_quotes(gate):
    gw.set_activity_paused(True)
    gw.set_activity_paused(False)
    assert gw.activity_paused() is False
    assert gw._QUOTE_LOOP_ENABLED is True
    assert gate.clear == [1]


def test_activity_gate_coerces_truthy_values(gate):
    gw.set_activity_paused(1)
    assert gw._ACTIVITY_PAUSED is True
    gw.set_activity_paused(0)
    assert gw._ACTIVITY_PAUSED is False


def test_activity_gate_swallows_feed_stop_errors(gate):
    gate.stop_raises = RuntimeError("feed store down")
    gate.clear_raises = RuntimeError("feed store down")
    gw.set_activity_paused(True)
    assert gw.activity_paused() is True and gw._QUOTE_LOOP_ENABLED is False
    gw.set_activity_paused(False)
    assert gw.activity_paused() is False and gw._QUOTE_LOOP_ENABLED is True


def test_scan_in_progress_reflects_flag(gate, monkeypatch):
    assert gw.scan_in_progress() is False
    monkeypatch.setattr(gw, "_SCAN_IN_PROGRESS", 1)
    assert gw.scan_in_progress() is True


# ── _graceful_shutdown_commit ────────────────────────────────────────────────

class FakeFeedStore:
    def __init__(self, job=None, job_raises=None, set_job_raises=None):
        self._job, self.job_raises, self.set_job_raises = job, job_raises, set_job_raises
        self.set_job_calls = []

    def job(self):
        if self.job_raises is not None:
            raise self.job_raises
        return self._job

    def set_job(self, **kw):
        if self.set_job_raises is not None:
            raise self.set_job_raises
        self.set_job_calls.append(kw)


class FakeWS:
    def __init__(self, close_raises=None):
        self.closed = 0
        self.close_raises = close_raises

    async def close(self):
        self.closed += 1


class FakeWSManager:
    def __init__(self, active, unwatch_raises=None):
        self.active = active
        self.unwatched = []
        self.unwatch_raises = unwatch_raises

    def unwatch_quotes(self, ws, symbols=None):
        self.unwatched.append((ws, symbols))
        if self.unwatch_raises is not None:
            raise self.unwatch_raises


@pytest.fixture
def commit_env(monkeypatch):
    env = types.SimpleNamespace()
    env.store = FakeFeedStore(job={"processed": 7, "ok_count": 5})
    env.hot_calls, env.redis_sets, env.feed_stops = [], [], []
    env.hot_raises = None
    env.redis_set_raises = None
    env.mem = {}

    def hot(set_fn, get_fn, **kw):
        env.hot_calls.append((set_fn, get_fn, kw))
        if env.hot_raises is not None:
            raise env.hot_raises

    def redis_set(key, value, ttl=None):
        if env.redis_set_raises is not None:
            raise env.redis_set_raises
        env.redis_sets.append((key, value, ttl))

    monkeypatch.setattr(gw, "_feed_store", lambda: env.store)
    monkeypatch.setattr(gw, "hot_job_set", hot)
    monkeypatch.setattr(gw, "_redis_set", redis_set)
    monkeypatch.setattr(gw, "request_data_feed_stop", lambda: env.feed_stops.append(1))
    monkeypatch.setattr(gw, "clear_data_feed_stop", lambda: None)
    monkeypatch.setattr(gw, "_mem_kv", env.mem)
    monkeypatch.setattr(gw, "_SCAN_CANCEL_FLAGS", set())
    monkeypatch.setattr(gw, "_ACTIVITY_PAUSED", False)
    monkeypatch.setattr(gw, "_QUOTE_LOOP_ENABLED", True)
    monkeypatch.setattr(gw, "ws_manager", FakeWSManager([]))
    monkeypatch.setattr(gw, "_quote_loop_task", None)
    return env


def _by_phase(phases):
    return {p["phase"]: p for p in phases}


def test_commit_happy_path_reports_all_six_phases(commit_env):
    phases = gw._graceful_shutdown_commit(reason="power_off")
    assert [p["phase"] for p in phases] == ["activity_gate", "scan", "data_feed", "hot_picks", "websocket", "quote_loop"]
    assert all(p["ok"] for p in phases)
    ph = _by_phase(phases)
    assert ph["activity_gate"]["detail"] == "paused (power_off)"
    assert ph["scan"]["detail"] == "cancel committed partial=0"
    assert gw.activity_paused() is True
    assert "__ALL__" in gw._SCAN_CANCEL_FLAGS


def test_commit_default_reason_is_shutdown(commit_env):
    phases = gw._graceful_shutdown_commit()
    assert _by_phase(phases)["activity_gate"]["detail"] == "paused (shutdown)"


def test_commit_marks_running_scans_cancelled_and_leaves_others(commit_env):
    p = gw.SCAN_TASK_PREFIX
    running = {"status": "running", "processed": 3}
    commit_env.mem.update({
        p + "t1": running,
        p + "t1:cancel": True,                                # cancel marker: never rewritten
        p + "t2": {"status": "done"},                         # finished: untouched
        p + "t3": "not-a-dict",                               # non-dict payload: untouched
        "stockky:other": {"status": "running"},               # not a scan task: untouched
    })
    phases = gw._graceful_shutdown_commit(reason="sigterm")
    assert _by_phase(phases)["scan"]["detail"] == "cancel committed partial=1"
    t1 = commit_env.mem[p + "t1"]
    assert t1["status"] == "cancelled" and t1["partial"] is True and t1["cancel_requested"] is True
    assert t1["message"] == "sigterm: scan stopped (partial committed)"
    assert t1["processed"] == 3
    assert running["status"] == "running"                     # original dict not mutated (copy is rewritten)
    assert commit_env.mem[p + "t2"] == {"status": "done"}
    assert commit_env.mem[p + "t3"] == "not-a-dict"
    assert commit_env.mem["stockky:other"] == {"status": "running"}
    assert (p + "t1", t1, 3600) in commit_env.redis_sets
    assert (p + "t1:cancel", True, 3600) in commit_env.redis_sets


def test_commit_counts_scan_even_when_redis_write_fails(commit_env):
    p = gw.SCAN_TASK_PREFIX
    commit_env.mem[p + "t1"] = {"status": "running"}
    commit_env.redis_set_raises = RuntimeError("kv down")
    phases = gw._graceful_shutdown_commit()
    assert _by_phase(phases)["scan"]["detail"] == "cancel committed partial=1"
    assert commit_env.mem[p + "t1"]["status"] == "cancelled"


def test_commit_scan_scan_of_mem_kv_failure_is_swallowed(commit_env, monkeypatch):
    class BadDict(dict):
        def keys(self):
            raise RuntimeError("iteration failed")

    monkeypatch.setattr(gw, "_mem_kv", BadDict())
    phases = gw._graceful_shutdown_commit()
    ph = _by_phase(phases)["scan"]
    assert ph["ok"] is True and ph["detail"] == "cancel committed partial=0"


def test_commit_scan_phase_reports_failure_when_flag_set_breaks(commit_env, monkeypatch):
    class BadSet(set):
        def add(self, item):
            raise RuntimeError("flag set frozen")

    monkeypatch.setattr(gw, "_SCAN_CANCEL_FLAGS", BadSet())
    ph = _by_phase(gw._graceful_shutdown_commit())["scan"]
    assert ph["ok"] is False and "flag set frozen" in ph["detail"]


def test_commit_activity_gate_failure_does_not_stop_other_phases(commit_env, monkeypatch):
    def broken(paused):
        raise RuntimeError("gate broken " + "x" * 300)

    monkeypatch.setattr(gw, "set_activity_paused", broken)
    phases = gw._graceful_shutdown_commit()
    ph = _by_phase(phases)
    assert ph["activity_gate"]["ok"] is False
    assert len(ph["activity_gate"]["detail"]) == 120           # detail is clipped
    assert all(ph[n]["ok"] for n in ("scan", "data_feed", "hot_picks", "websocket", "quote_loop"))


def test_commit_data_feed_checkpoint_carries_progress(commit_env):
    gw._graceful_shutdown_commit(reason="power_off")
    assert commit_env.feed_stops == [1, 1]          # once from the activity gate, once from the feed phase
    (kw,) = commit_env.store.set_job_calls
    assert kw["status"] == "stopped" and kw["stop_requested"] is True
    assert kw["message"] == "power_off: data feed stopped (checkpoint committed)"
    assert kw["processed"] == 7 and kw["ok_count"] == 5
    assert isinstance(kw["finished_at"], str) and "T" in kw["finished_at"]


def test_commit_data_feed_ok_count_falls_back_to_processed(commit_env):
    commit_env.store = FakeFeedStore(job={"processed": 9})
    gw._graceful_shutdown_commit()
    assert commit_env.store.set_job_calls[0]["ok_count"] == 9


def test_commit_data_feed_handles_missing_job(commit_env):
    commit_env.store = FakeFeedStore(job=None)
    phases = gw._graceful_shutdown_commit()
    assert _by_phase(phases)["data_feed"]["ok"] is True
    kw = commit_env.store.set_job_calls[0]
    assert kw["processed"] == 0 and kw["ok_count"] == 0


def test_commit_data_feed_tolerates_feed_stop_error_but_reports_store_error(commit_env, monkeypatch):
    def bad_stop():
        raise RuntimeError("no stop flag store")

    monkeypatch.setattr(gw, "request_data_feed_stop", bad_stop)
    assert _by_phase(gw._graceful_shutdown_commit())["data_feed"]["ok"] is True
    commit_env.store = FakeFeedStore(set_job_raises=RuntimeError("neon down"))
    ph = _by_phase(gw._graceful_shutdown_commit())["data_feed"]
    assert ph["ok"] is False and "neon down" in ph["detail"]


def test_commit_data_feed_reports_store_construction_failure(commit_env, monkeypatch):
    def boom():
        raise RuntimeError("no store")

    monkeypatch.setattr(gw, "_feed_store", boom)
    assert _by_phase(gw._graceful_shutdown_commit())["data_feed"]["ok"] is False


def test_commit_hot_picks_goes_idle(commit_env):
    gw._graceful_shutdown_commit(reason="power_off")
    ((set_fn, get_fn, kw),) = commit_env.hot_calls
    assert set_fn is gw._redis_set or callable(set_fn)
    assert kw == {"status": "idle", "message": "power_off: Hot Picks stopped",
                  "processed": 0, "estimated_remaining_sec": 0}


def test_commit_hot_picks_failure_is_reported(commit_env):
    commit_env.hot_raises = RuntimeError("hot store down")
    ph = _by_phase(gw._graceful_shutdown_commit())["hot_picks"]
    assert ph["ok"] is False and "hot store down" in ph["detail"]


def test_commit_websocket_unwatches_and_closes_without_running_loop(commit_env, monkeypatch):
    ws1, ws2 = FakeWS(), FakeWS()
    mgr = FakeWSManager([ws1, ws2])
    monkeypatch.setattr(gw, "ws_manager", mgr)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        ph = _by_phase(gw._graceful_shutdown_commit())["websocket"]
    finally:
        asyncio.set_event_loop(None)
        loop.close()
    assert ph["ok"] is True and ph["detail"] == "unwatched + close signalled"
    assert mgr.unwatched == [(ws1, None), (ws2, None)]
    assert (ws1.closed, ws2.closed) == (1, 1)


def test_commit_websocket_schedules_close_when_loop_is_running(commit_env, monkeypatch):
    ws = FakeWS()
    monkeypatch.setattr(gw, "ws_manager", FakeWSManager([ws]))

    async def scenario():
        phases = gw._graceful_shutdown_commit()
        await asyncio.sleep(0)                      # let the scheduled close() task run
        await asyncio.sleep(0)
        return phases

    phases = _run(scenario())
    assert _by_phase(phases)["websocket"]["ok"] is True
    assert ws.closed == 1


def test_commit_websocket_survives_per_socket_errors(commit_env, monkeypatch):
    class BadCloseWS:
        def close(self):
            raise RuntimeError("close() failed synchronously")

    good = FakeWS()
    mgr = FakeWSManager([BadCloseWS(), good], unwatch_raises=RuntimeError("unwatch failed"))
    monkeypatch.setattr(gw, "ws_manager", mgr)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        ph = _by_phase(gw._graceful_shutdown_commit())["websocket"]
    finally:
        asyncio.set_event_loop(None)
        loop.close()
    assert ph["ok"] is True
    assert len(mgr.unwatched) == 2                  # one bad socket did not stop the sweep
    assert good.closed == 1


def test_commit_websocket_with_no_active_attribute(commit_env, monkeypatch):
    monkeypatch.setattr(gw, "ws_manager", object())
    assert _by_phase(gw._graceful_shutdown_commit())["websocket"]["ok"] is True


def test_commit_websocket_phase_reports_manager_failure(commit_env, monkeypatch):
    class BadMgr:
        @property
        def active(self):
            raise RuntimeError("manager gone")

    monkeypatch.setattr(gw, "ws_manager", BadMgr())
    ph = _by_phase(gw._graceful_shutdown_commit())["websocket"]
    assert ph["ok"] is False and "manager gone" in ph["detail"]


def test_commit_websocket_outer_guard_when_asyncio_cannot_be_imported(commit_env, monkeypatch):
    """Defensive-only guard: `import asyncio as _aio` inside the close loop cannot fail in a real
    process, so the outer `except Exception: pass` is unreachable with real data. Contrived here
    by making the import raise; the sweep must still finish and report ok."""
    mgr = FakeWSManager([FakeWS(), FakeWS()])
    monkeypatch.setattr(gw, "ws_manager", mgr)
    monkeypatch.setitem(sys.modules, "asyncio", None)
    phases = gw._graceful_shutdown_commit()
    assert _by_phase(phases)["websocket"]["ok"] is True
    assert len(mgr.unwatched) == 2


def test_commit_quote_loop_task_is_cancelled_only_when_running(commit_env, monkeypatch):
    class Task:
        def __init__(self, done):
            self._done, self.cancelled = done, 0

        def done(self):
            return self._done

        def cancel(self):
            self.cancelled += 1

    live, finished = Task(False), Task(True)
    monkeypatch.setattr(gw, "_quote_loop_task", live)
    assert _by_phase(gw._graceful_shutdown_commit())["quote_loop"] == {"phase": "quote_loop", "ok": True, "detail": "cancelled"}
    assert live.cancelled == 1
    monkeypatch.setattr(gw, "_quote_loop_task", finished)
    gw._graceful_shutdown_commit()
    assert finished.cancelled == 0
    monkeypatch.setattr(gw, "_quote_loop_task", object())       # no done()/cancel(): left alone
    assert _by_phase(gw._graceful_shutdown_commit())["quote_loop"]["ok"] is True


def test_commit_quote_loop_failure_is_reported(commit_env, monkeypatch):
    class Task:
        def done(self):
            raise RuntimeError("done exploded")

    monkeypatch.setattr(gw, "_quote_loop_task", Task())
    ph = _by_phase(gw._graceful_shutdown_commit())["quote_loop"]
    assert ph["ok"] is False and "done exploded" in ph["detail"]


def test_commit_sees_running_scans_written_through_kv_cache(commit_env, monkeypatch):
    """FIXED: scan-task status is written through `_redis_set`, which returns right after a successful
    `kv_cache.set` and used to never populate `_mem_kv` when kv_cache is importable (the normal production
    state). `_graceful_shutdown_commit` (and `/scan/stop-all`) only walk `_mem_kv`, so they found 0 running
    scans and the persisted status stayed "running" until its TTL. `_redis_set` now mirrors a scan-task key
    into `_mem_kv` while its status is "running", so the sweep finds it and commits the partial."""
    kv = FakeKVCache()
    # commit_env swapped `_redis_set` for a recorder; put the real one back for this test
    monkeypatch.setattr(gw, "_redis_set", _REAL_REDIS_SET)
    monkeypatch.setattr(gw, "_kv_cache", kv)
    mem = {}
    monkeypatch.setattr(gw, "_mem_kv", mem)
    monkeypatch.setattr(gw, "_mem_kv_exp", {})
    key = gw.SCAN_TASK_PREFIX + "abc"
    gw._redis_set(key, {"status": "running"}, ttl=3600)
    assert kv.store[key] == {"status": "running"}
    assert mem[key] == {"status": "running"}                      # mirrored while running
    phases = gw._graceful_shutdown_commit()
    assert _by_phase(phases)["scan"]["detail"] == "cancel committed partial=1"
    done = kv.store[key]
    assert done["status"] == "cancelled" and done["partial"] is True and done["cancel_requested"] is True
    assert kv.store[key + ":cancel"] is True
    assert "__ALL__" in gw._SCAN_CANCEL_FLAGS
    assert key not in mem                                         # the cancelled write drops the mirror


def test_scan_stop_all_sees_running_scans_written_through_kv_cache(monkeypatch):
    kv = FakeKVCache()
    monkeypatch.setattr(gw, "_kv_cache", kv)
    monkeypatch.setattr(gw, "_mem_kv", {})
    monkeypatch.setattr(gw, "_mem_kv_exp", {})
    monkeypatch.setattr(gw, "_SCAN_CANCEL_FLAGS", set())
    k1, k2 = gw.SCAN_TASK_PREFIX + "a", gw.SCAN_TASK_PREFIX + "b"
    gw._redis_set(k1, {"status": "running", "processed": 3}, ttl=3600)
    gw._redis_set(k2, {"status": "done"}, ttl=3600)
    out = gw.scan_stop_all()
    assert out["stopped"] == 1 and "durable_write_failures" not in out
    assert kv.store[k1]["status"] == "cancelled" and kv.store[k1]["partial"] is True
    assert kv.store[k2]["status"] == "done"                        # finished scans are left alone


def test_scan_task_mirror_is_bounded_to_running_scans(monkeypatch):
    kv = FakeKVCache()
    monkeypatch.setattr(gw, "_kv_cache", kv)
    mem, exp = {}, {}
    monkeypatch.setattr(gw, "_mem_kv", mem)
    monkeypatch.setattr(gw, "_mem_kv_exp", exp)
    k1, k2 = gw.SCAN_TASK_PREFIX + "t1", gw.SCAN_TASK_PREFIX + "t2"
    gw._redis_set(k1, {"status": "running", "processed": 1}, ttl=60)
    gw._redis_set(k2, {"status": "running"})                      # no ttl -> no expiry entry
    assert set(mem) == {k1, k2} and k1 in exp and k2 not in exp
    gw._redis_set(k1, {"status": "done"}, ttl=60)                 # finished -> dropped
    assert set(mem) == {k2} and k1 not in exp
    gw._redis_set(k2 + ":cancel", True, ttl=60)                   # cancel flag is never mirrored
    gw._redis_set("stockky:other", {"status": "running"}, ttl=60) # non-scan keys never mirrored
    assert set(mem) == {k2}
    mem[k1] = {"status": "running"}; exp[k1] = 1.0                # a stale, long-expired mirror entry
    gw._redis_set(k2, {"status": "running", "processed": 2})      # ...is pruned on the next scan write
    assert set(mem) == {k2} and k1 not in exp
    # the mirror is a copy: mutating the caller's dict later does not change it
    payload = {"status": "running"}
    gw._redis_set(k2, payload)
    payload["status"] = "done"
    assert mem[k2]["status"] == "running"


def test_scan_task_mirror_never_raises(monkeypatch):
    monkeypatch.setattr(gw, "_kv_cache", FakeKVCache())
    monkeypatch.setattr(gw, "_mem_kv", None)                      # makes the helper's own bookkeeping blow up
    gw._redis_set(gw.SCAN_TASK_PREFIX + "x", {"status": "running"}, ttl=60)   # must not raise
    gw._sync_scan_task_mirror(123, {"status": "running"})         # non-str key: ignored


_REAL_REDIS_SET = gw._redis_set


# ── _feed_store ──────────────────────────────────────────────────────────────

class FakeStoreCls:
    instances = []

    def __init__(self, get_fn, set_fn, redis):
        self.args = (get_fn, set_fn, redis)
        self.calls = []
        self.warm_raises = None
        FakeStoreCls.instances.append(self)

    def meta(self):
        self.calls.append("meta")
        if self.warm_raises is not None:
            raise self.warm_raises

    def job(self):
        self.calls.append("job")

    def list_symbols(self):
        self.calls.append("list_symbols")


def test_feed_store_is_built_once_and_warmed(monkeypatch):
    FakeStoreCls.instances = []
    sentinel = object()
    monkeypatch.setattr(gw, "DataFeedStore", FakeStoreCls)
    monkeypatch.setattr(gw, "_data_feed_store", None)
    monkeypatch.setattr(gw, "_redis", sentinel)
    s1 = gw._feed_store()
    s2 = gw._feed_store()
    assert s1 is s2 and len(FakeStoreCls.instances) == 1
    assert s1.args == (gw._redis_get, gw._redis_set, sentinel)
    assert s1.calls == ["meta", "job", "list_symbols"]         # warmed exactly once


def test_feed_store_survives_warm_up_failure(monkeypatch):
    class Cold(FakeStoreCls):
        def __init__(self, *a):
            super().__init__(*a)
            self.warm_raises = RuntimeError("neon cold")

    monkeypatch.setattr(gw, "DataFeedStore", Cold)
    monkeypatch.setattr(gw, "_data_feed_store", None)
    s = gw._feed_store()
    assert isinstance(s, Cold) and gw._data_feed_store is s


# ── _redis_get ───────────────────────────────────────────────────────────────

@pytest.fixture
def mem_env(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(gw, "_time_mod", clock)
    monkeypatch.setattr(gw, "_mem_kv", {})
    monkeypatch.setattr(gw, "_mem_kv_exp", {})
    monkeypatch.setattr(gw, "_kv_cache", None)
    monkeypatch.setattr(gw, "_redis", None)
    return clock


def test_redis_get_prefers_kv_cache(mem_env, monkeypatch):
    kv = FakeKVCache()
    kv.store["k"] = {"v": 1}
    monkeypatch.setattr(gw, "_kv_cache", kv)
    gw._mem_kv["k"] = "memory copy"
    assert gw._redis_get("k") == {"v": 1}


def test_redis_get_kv_miss_is_final_and_does_not_consult_memory_or_redis(mem_env, monkeypatch):
    kv = FakeKVCache()
    monkeypatch.setattr(gw, "_kv_cache", kv)
    gw._mem_kv["k"] = "memory copy"
    client = FakeRedisClient()
    client.data["k"] = "redis copy"
    monkeypatch.setattr(gw, "_redis", client)
    assert gw._redis_get("k") is None


def test_redis_get_falls_back_to_memory_when_kv_raises(mem_env, monkeypatch):
    kv = FakeKVCache()
    kv.get_raises = RuntimeError("neon down")
    monkeypatch.setattr(gw, "_kv_cache", kv)
    gw._mem_kv["k"] = {"cached": True}
    assert gw._redis_get("k") == {"cached": True}


def test_redis_get_memory_entry_without_expiry_is_returned(mem_env):
    gw._mem_kv["k"] = "forever"
    assert gw._redis_get("k") == "forever"


def test_redis_get_memory_entry_before_expiry_is_returned(mem_env):
    gw._mem_kv["k"] = "fresh"
    gw._mem_kv_exp["k"] = mem_env.now + 10
    assert gw._redis_get("k") == "fresh"


def test_redis_get_expired_memory_entry_is_evicted(mem_env):
    gw._mem_kv["k"] = "stale"
    gw._mem_kv_exp["k"] = mem_env.now - 1
    assert gw._redis_get("k") is None
    assert "k" not in gw._mem_kv and "k" not in gw._mem_kv_exp


def test_redis_get_expired_memory_entry_does_not_fall_through_to_redis(mem_env, monkeypatch):
    client = FakeRedisClient()
    client.data["k"] = json.dumps({"from": "redis"})
    monkeypatch.setattr(gw, "_redis", client)
    gw._mem_kv["k"] = "stale"
    gw._mem_kv_exp["k"] = mem_env.now - 1
    assert gw._redis_get("k") is None


def test_redis_get_unreadable_memory_falls_through_to_redis(mem_env, monkeypatch):
    class BadExp(dict):
        def get(self, k, d=None):
            raise RuntimeError("exp table corrupt")

    monkeypatch.setattr(gw, "_mem_kv_exp", BadExp())
    client = FakeRedisClient()
    client.data["k"] = json.dumps({"a": 1})
    monkeypatch.setattr(gw, "_redis", client)
    assert gw._redis_get("k") == {"a": 1}


@pytest.mark.parametrize("raw,expected", [
    (json.dumps({"a": 1}), {"a": 1}),            # str JSON
    (json.dumps([1, 2]).encode(), [1, 2]),       # bytes JSON
    ("plain text", "plain text"),                # str that is not JSON -> returned raw
    (b"\xff\xfe", b"\xff\xfe"),                  # bytes that are not JSON -> returned raw
    ({"already": "parsed"}, {"already": "parsed"}),   # non str/bytes -> returned as is
    (42, 42),
])
def test_redis_get_decodes_redis_values(mem_env, monkeypatch, raw, expected):
    client = FakeRedisClient()
    client.data["k"] = raw
    monkeypatch.setattr(gw, "_redis", client)
    assert gw._redis_get("k") == expected


def test_redis_get_redis_miss_and_errors_return_none(mem_env, monkeypatch):
    client = FakeRedisClient()
    monkeypatch.setattr(gw, "_redis", client)
    assert gw._redis_get("absent") is None
    client.get_raises = ConnectionError("upstash down")
    assert gw._redis_get("absent") is None


def test_redis_get_with_nothing_configured_returns_none(mem_env):
    assert gw._redis_get("anything") is None


# ── _redis_soft_ttl_refresh ──────────────────────────────────────────────────

def test_soft_ttl_refresh_false_without_redis(mem_env):
    assert gw._redis_soft_ttl_refresh("k") is False


@pytest.mark.parametrize("ttl,window,expected", [
    (5, 10, True), (10, 10, True), (1, 10, True),
    (11, 10, False), (0, 10, False), (-1, 10, False), (-2, 10, False),
    (None, 10, False), ("5", 10, False), (30, 60, True),
])
def test_soft_ttl_refresh_window(mem_env, monkeypatch, ttl, window, expected):
    client = FakeRedisClient()
    client.ttl_value = ttl
    monkeypatch.setattr(gw, "_redis", client)
    assert gw._redis_soft_ttl_refresh("k", soft_window=window) is expected


def test_soft_ttl_refresh_default_window_and_error(mem_env, monkeypatch):
    client = FakeRedisClient()
    client.ttl_value = 10
    monkeypatch.setattr(gw, "_redis", client)
    assert gw._redis_soft_ttl_refresh("k") is True             # default window is 10s
    client.ttl_raises = RuntimeError("ttl failed")
    assert gw._redis_soft_ttl_refresh("k") is False


# ── _redis_set ───────────────────────────────────────────────────────────────

def test_redis_set_uses_kv_cache_and_stops(mem_env, monkeypatch):
    kv = FakeKVCache()
    client = FakeRedisClient()
    monkeypatch.setattr(gw, "_kv_cache", kv)
    monkeypatch.setattr(gw, "_redis", client)
    gw._redis_set("k", {"v": 1}, ttl=60)
    assert kv.set_calls == [("k", {"v": 1}, 60)]
    assert gw._mem_kv == {} and client.calls == []


def test_redis_set_kv_failure_falls_back_to_memory_and_redis(mem_env, monkeypatch):
    kv = FakeKVCache()
    kv.set_raises = RuntimeError("neon down")
    client = FakeRedisClient()
    monkeypatch.setattr(gw, "_kv_cache", kv)
    monkeypatch.setattr(gw, "_redis", client)
    gw._redis_set("k", {"v": 1}, ttl=30)
    assert gw._mem_kv["k"] == {"v": 1}
    assert client.calls == [("setex", "k", 30, json.dumps({"v": 1}))]


def test_redis_set_memory_ttl_and_no_ttl(mem_env):
    gw._redis_set("k", "v1", ttl=100)
    assert gw._mem_kv["k"] == "v1" and gw._mem_kv_exp["k"] == mem_env.now + 100
    gw._redis_set("k", "v2")                                    # no ttl: expiry cleared
    assert gw._mem_kv["k"] == "v2" and "k" not in gw._mem_kv_exp
    gw._redis_set("k2", "v", ttl="45")                          # ttl given as text is coerced
    assert gw._mem_kv_exp["k2"] == mem_env.now + 45


def test_redis_set_then_get_round_trip_and_expiry(mem_env):
    gw._redis_set("k", {"n": 1}, ttl=5)
    assert gw._redis_get("k") == {"n": 1}
    mem_env.now += 6
    assert gw._redis_get("k") is None


def test_redis_set_soft_cap_evicts_oldest_500(mem_env, monkeypatch):
    big = {f"k{i}": i for i in range(8000)}
    exp = {f"k{i}": 1.0 for i in range(8000)}
    monkeypatch.setattr(gw, "_mem_kv", big)
    monkeypatch.setattr(gw, "_mem_kv_exp", exp)
    gw._redis_set("new", "x")                                   # 8001 entries > 8000 -> trim 500
    assert len(big) == 7501
    assert "k0" not in big and "k499" not in big and "k500" in big and "new" in big
    assert "k0" not in exp and "k499" not in exp


def test_redis_set_at_cap_boundary_does_not_evict(mem_env, monkeypatch):
    big = {f"k{i}": i for i in range(7999)}
    monkeypatch.setattr(gw, "_mem_kv", big)
    gw._redis_set("new", "x")                                   # exactly 8000: no eviction
    assert len(big) == 8000 and "k0" in big


def test_redis_set_redis_payload_shapes(mem_env, monkeypatch):
    client = FakeRedisClient()
    monkeypatch.setattr(gw, "_redis", client)
    gw._redis_set("a", "already-a-string")                      # strings are sent as is
    gw._redis_set("b", {"n": 1}, ttl=9)
    gw._redis_set("c", {"when": gw.datetime(2026, 1, 2)}, ttl=None)   # default=str handles non-JSON types
    assert client.calls[0] == ("set", "a", "already-a-string")
    assert client.calls[1] == ("setex", "b", 9, json.dumps({"n": 1}))
    assert client.calls[2][0] == "set" and "2026-01-02" in client.calls[2][2]


def test_redis_set_redis_failure_is_swallowed(mem_env, monkeypatch):
    client = FakeRedisClient()
    client.write_raises = ConnectionError("upstash down")
    monkeypatch.setattr(gw, "_redis", client)
    gw._redis_set("k", "v", ttl=5)
    assert gw._mem_kv["k"] == "v"                               # memory write still landed


def test_redis_set_memory_failure_is_swallowed_and_redis_still_written(mem_env, monkeypatch):
    class Frozen(dict):
        def __setitem__(self, k, v):
            raise RuntimeError("memory table frozen")

    client = FakeRedisClient()
    monkeypatch.setattr(gw, "_mem_kv", Frozen())
    monkeypatch.setattr(gw, "_redis", client)
    gw._redis_set("k", "v")
    assert client.calls == [("set", "k", "v")]


# ── watchlist / searched helpers ─────────────────────────────────────────────

def _kv_module(monkeypatch, kv):
    m = types.ModuleType("kv_cache")
    m.watchlist_get, m.watchlist_set = kv.watchlist_get, kv.watchlist_set
    monkeypatch.setitem(sys.modules, "kv_cache", m)
    return m


def test_load_watchlist_list_is_normalised(monkeypatch):
    kv = FakeKVCache()
    kv.watchlist_value = ["reliance.ns", " tcs.bo ", "INFY", "", None, 123]
    _kv_module(monkeypatch, kv)
    assert gw._load_watchlist() == ["RELIANCE", "TCS", "INFY", "123"]


def test_load_watchlist_accepts_dict_with_symbols(monkeypatch):
    kv = FakeKVCache()
    kv.watchlist_value = {"symbols": ["hdfcbank.ns", None, "sbin"]}
    _kv_module(monkeypatch, kv)
    assert gw._load_watchlist() == ["HDFCBANK", "SBIN"]


def test_load_watchlist_empty_durable_list_is_authoritative(monkeypatch):
    kv = FakeKVCache()
    kv.watchlist_value = []
    _kv_module(monkeypatch, kv)
    monkeypatch.setattr(gw, "_redis_get", lambda k: ["LEGACY"])
    assert gw._load_watchlist() == []


@pytest.mark.parametrize("durable", [None, "oops", {"symbols": "not-a-list"}, {"other": 1}, 5])
def test_load_watchlist_unusable_durable_value_uses_legacy_key(monkeypatch, durable):
    kv = FakeKVCache()
    kv.watchlist_value = durable
    _kv_module(monkeypatch, kv)
    seen = []
    monkeypatch.setattr(gw, "_redis_get", lambda k: seen.append(k) or ["legacy.ns", "", "wipro"])
    assert gw._load_watchlist() == ["LEGACY", "WIPRO"]
    assert seen == [gw.WATCHLIST_KEY]


def test_load_watchlist_legacy_non_list_and_missing(monkeypatch):
    kv = FakeKVCache()
    _kv_module(monkeypatch, kv)
    monkeypatch.setattr(gw, "_redis_get", lambda k: {"not": "a list"})
    assert gw._load_watchlist() == []
    monkeypatch.setattr(gw, "_redis_get", lambda k: None)
    assert gw._load_watchlist() == []


def test_load_watchlist_durable_error_or_missing_module_uses_legacy(monkeypatch):
    kv = FakeKVCache()
    kv.watchlist_get_raises = RuntimeError("neon down")
    _kv_module(monkeypatch, kv)
    monkeypatch.setattr(gw, "_redis_get", lambda k: ["abc"])
    assert gw._load_watchlist() == ["ABC"]
    monkeypatch.setitem(sys.modules, "kv_cache", None)          # import itself fails
    assert gw._load_watchlist() == ["ABC"]


def test_save_watchlist_normalises_and_persists(monkeypatch):
    kv = FakeKVCache()
    _kv_module(monkeypatch, kv)
    gw._save_watchlist(["reliance.ns", " tcs.bo", "", None, "infy"])
    assert kv.watchlist_sets == [["RELIANCE", "TCS", "INFY"]]


def test_save_watchlist_none_saves_empty_list(monkeypatch):
    kv = FakeKVCache()
    _kv_module(monkeypatch, kv)
    gw._save_watchlist(None)
    assert kv.watchlist_sets == [[]]


def test_save_watchlist_failure_writes_legacy_key(monkeypatch):
    kv = FakeKVCache()
    kv.watchlist_set_raises = RuntimeError("table missing")
    _kv_module(monkeypatch, kv)
    writes = []
    monkeypatch.setattr(gw, "_redis_set", lambda k, v, ttl=None: writes.append((k, v, ttl)))
    gw._save_watchlist(["sbin.ns"])
    assert writes == [(gw.WATCHLIST_KEY, ["SBIN"], None)]


def test_watchlist_round_trip_through_durable_store(monkeypatch):
    kv = FakeKVCache()
    m = _kv_module(monkeypatch, kv)
    m.watchlist_set = lambda syms: setattr(kv, "watchlist_value", list(syms))
    gw._save_watchlist(["tcs.ns", "INFY"])
    assert gw._load_watchlist() == ["TCS", "INFY"]


def test_load_searched_defaults_to_empty_list(monkeypatch):
    monkeypatch.setattr(gw, "_redis_get", lambda k: None)
    assert gw._load_searched() == []
    monkeypatch.setattr(gw, "_redis_get", lambda k: ["A", "B"])
    assert gw._load_searched() == ["A", "B"]


def test_add_searched_appends_normalised_symbol(monkeypatch):
    writes = []
    monkeypatch.setattr(gw, "_redis_get", lambda k: ["TCS"])
    monkeypatch.setattr(gw, "_redis_set", lambda k, v, ttl=None: writes.append((k, list(v), ttl)))
    gw._add_searched("reliance.ns")
    assert writes == [(gw.SEARCHED_KEY, ["TCS", "RELIANCE"], None)]


def test_add_searched_ignores_duplicates_after_normalising(monkeypatch):
    writes = []
    monkeypatch.setattr(gw, "_redis_get", lambda k: ["TCS"])
    monkeypatch.setattr(gw, "_redis_set", lambda *a, **k: writes.append(a))
    gw._add_searched("tcs.bo")
    gw._add_searched("TCS")
    assert writes == []


def test_add_searched_keeps_only_the_newest_200(monkeypatch):
    writes = []
    monkeypatch.setattr(gw, "_redis_get", lambda k: [f"S{i}" for i in range(205)])
    monkeypatch.setattr(gw, "_redis_set", lambda k, v, ttl=None: writes.append(list(v)))
    gw._add_searched("NEW")
    (saved,) = writes
    assert len(saved) == 200 and saved[-1] == "NEW" and saved[0] == "S6"


def test_add_searched_strips_whitespace_and_dedupes(monkeypatch):
    """FIXED: `_add_searched` never `.strip()`-ed, so " tcs.ns " was stored as " TCS " and did not
    de-duplicate against "TCS". It now strips (before and after the suffix removal) and ignores blanks."""
    writes = []
    monkeypatch.setattr(gw, "_redis_get", lambda k: ["TCS"])
    monkeypatch.setattr(gw, "_redis_set", lambda k, v, ttl=None: writes.append(list(v)))
    gw._add_searched(" tcs.ns ")                                   # same symbol as stored -> no write
    assert writes == []
    gw._add_searched(" infy.ns ")
    assert writes == [["TCS", "INFY"]]
    gw._add_searched("   ")                                        # blank -> ignored, not stored as ""
    gw._add_searched("")
    gw._add_searched(None)
    assert writes == [["TCS", "INFY"]]
    gw._add_searched("reliance .bo")                               # whitespace left after the suffix is gone
    assert writes[-1] == ["TCS", "RELIANCE"]


# ── NSE client / API ─────────────────────────────────────────────────────────

class FakeNseClient:
    instances = []
    script = {}                   # url -> list of responses (or exceptions) consumed in order
    bootstrap_cookies = ("nsit", "nseappid")

    def __init__(self, **kw):
        self.kw = kw
        self.cookies = {n: "x" for n in FakeNseClient.bootstrap_cookies}
        self.gets = []
        self.closed = False
        self.close_raises = None
        self.bootstrap_raises = None
        FakeNseClient.instances.append(self)

    def get(self, url, headers=None):
        self.gets.append((url, headers))
        if url == "https://www.nseindia.com":
            if self.bootstrap_raises is not None:
                raise self.bootstrap_raises
            return types.SimpleNamespace(status_code=200)
        item = FakeNseClient.script[url].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True
        if self.close_raises is not None:
            raise self.close_raises


class Resp:
    def __init__(self, status=200, data=None, json_raises=None):
        self.status_code, self._data, self._raises = status, data, json_raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._data


@pytest.fixture
def nse(monkeypatch):
    FakeNseClient.instances = []
    FakeNseClient.script = {}
    FakeNseClient.bootstrap_cookies = ("nsit", "nseappid")
    clock = Clock(5000.0)
    monkeypatch.setattr(gw.httpx, "Client", FakeNseClient)
    monkeypatch.setattr(gw, "time", clock)
    monkeypatch.setattr(gw, "_nse_client", None)
    monkeypatch.setattr(gw, "_nse_client_ts", 0.0)
    return clock


def test_nse_client_ttl_matches_ipo_scanner_session_ttl():
    import ipo_scanner
    assert gw._NSE_CLIENT_TTL_SECONDS == ipo_scanner._NSE_SESSION_TTL_SECONDS == 300


def test_nse_client_bootstraps_with_browser_headers(nse):
    c = gw._get_nse_client()
    assert c is FakeNseClient.instances[0] and gw._nse_client is c
    assert c.kw == {"headers": gw._NSE_CLIENT_HEADERS, "timeout": 15, "follow_redirects": True}
    assert c.gets == [("https://www.nseindia.com", gw._NSE_CLIENT_BOOTSTRAP_HEADERS)]
    assert gw._nse_client_ts == nse.now


def test_nse_client_reused_within_ttl(nse):
    c1 = gw._get_nse_client()
    nse.now += gw._NSE_CLIENT_TTL_SECONDS - 1
    assert gw._get_nse_client() is c1 and len(FakeNseClient.instances) == 1


def test_nse_client_rebuilt_after_ttl_and_old_one_closed(nse):
    c1 = gw._get_nse_client()
    nse.now += gw._NSE_CLIENT_TTL_SECONDS
    c2 = gw._get_nse_client()
    assert c2 is not c1 and c1.closed is True and gw._nse_client is c2
    assert not c2.closed


def test_nse_client_force_new_ignores_a_fresh_session(nse):
    c1 = gw._get_nse_client()
    c2 = gw._get_nse_client(force_new=True)
    assert c2 is not c1 and c1.closed


def test_nse_client_close_failure_of_old_session_is_ignored(nse):
    c1 = gw._get_nse_client()
    c1.close_raises = RuntimeError("already closed")
    c2 = gw._get_nse_client(force_new=True)
    assert gw._nse_client is c2


def test_nse_client_weak_cookies_are_logged_but_session_is_kept(nse, monkeypatch):
    log = RecLogger()
    monkeypatch.setattr(gw, "logger", log)
    FakeNseClient.bootstrap_cookies = ("anon",)
    c = gw._get_nse_client()
    assert gw._nse_client is c
    assert len(log.infos) == 1 and "bootstrap cookies weak (anon; status 200)" in log.infos[0]


def test_nse_client_strong_cookies_log_nothing(nse, monkeypatch):
    log = RecLogger()
    monkeypatch.setattr(gw, "logger", log)
    gw._get_nse_client()
    assert log.infos == []


@pytest.mark.parametrize("only", ["nsit", "nseappid"])
def test_nse_client_either_real_cookie_counts_as_strong(nse, monkeypatch, only):
    log = RecLogger()
    monkeypatch.setattr(gw, "logger", log)
    FakeNseClient.bootstrap_cookies = (only,)
    gw._get_nse_client()
    assert log.infos == []


def test_nse_client_bootstrap_failure_still_returns_a_client(nse, monkeypatch):
    orig_init = FakeNseClient.__init__

    def init(self, **kw):
        orig_init(self, **kw)
        self.bootstrap_raises = RuntimeError("nse unreachable")

    monkeypatch.setattr(FakeNseClient, "__init__", init)
    c = gw._get_nse_client()
    assert c is gw._nse_client and gw._nse_client_ts == nse.now


@pytest.fixture
def api(monkeypatch, nse):
    env = types.SimpleNamespace(cache={}, sets=[], clients=[], client_calls=[])
    monkeypatch.setattr(gw, "_redis_get", lambda k: env.cache.get(k))
    monkeypatch.setattr(gw, "_redis_set", lambda k, v, ttl=None: env.sets.append((k, v, ttl)))

    def fake_get_client(force_new=False):
        env.client_calls.append(force_new)
        return env.clients.pop(0)

    monkeypatch.setattr(gw, "_get_nse_client", fake_get_client)
    return env


class ScriptedClient:
    def __init__(self, *responses):
        self.responses, self.urls = list(responses), []

    def get(self, url):
        self.urls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_fetch_from_nse_api_returns_cached_dict_without_network(api):
    api.cache["ck"] = {"cached": 1}
    assert gw._fetch_from_nse_api("equity-stockIndices?index=NIFTY%2050", "ck") == {"cached": 1}
    assert api.client_calls == []


def test_fetch_from_nse_api_success_caches_with_ttl(api):
    c = ScriptedClient(Resp(200, {"data": [1]}))
    api.clients = [c]
    out = gw._fetch_from_nse_api("market-data-pre-open", "ck", ttl=123)
    assert out == {"data": [1]}
    assert c.urls == ["https://www.nseindia.com/api/market-data-pre-open"]
    assert api.sets == [("ck", {"data": [1]}, 123)]
    assert api.client_calls == [False]


def test_fetch_from_nse_api_default_ttl_is_six_hours(api):
    api.clients = [ScriptedClient(Resp(200, {"x": 1}))]
    gw._fetch_from_nse_api("e", "ck")
    assert api.sets[0][2] == 21600


@pytest.mark.parametrize("bad_status", [401, 403])
def test_fetch_from_nse_api_refreshes_session_once_on_blocked_status(api, bad_status):
    first = ScriptedClient(Resp(bad_status))
    second = ScriptedClient(Resp(200, {"ok": True}))
    api.clients = [first, second]
    assert gw._fetch_from_nse_api("e", "ck") == {"ok": True}
    assert api.client_calls == [False, True]                   # second call forces a fresh bootstrap
    assert len(first.urls) == 1 and len(second.urls) == 1


def test_fetch_from_nse_api_gives_up_when_retry_is_still_blocked(api):
    api.clients = [ScriptedClient(Resp(403)), ScriptedClient(Resp(403))]
    assert gw._fetch_from_nse_api("e", "ck") is None
    assert api.sets == []


def test_fetch_from_nse_api_other_status_returns_none(api):
    api.clients = [ScriptedClient(Resp(500))]
    assert gw._fetch_from_nse_api("e", "ck") is None
    assert api.client_calls == [False]                          # 500 does not trigger a session refresh


def test_fetch_from_nse_api_non_dict_body_is_not_cached(api):
    api.clients = [ScriptedClient(Resp(200, ["a", "list"]))]
    assert gw._fetch_from_nse_api("e", "ck") is None
    assert api.sets == []


def test_fetch_from_nse_api_ignores_non_dict_cache_entry(api):
    api.cache["ck"] = ["legacy list"]
    api.clients = [ScriptedClient(Resp(200, {"fresh": True}))]
    assert gw._fetch_from_nse_api("e", "ck") == {"fresh": True}


def test_fetch_from_nse_api_returns_stale_cache_value_on_failure(api):
    api.cache["ck"] = ["legacy list"]                           # truthy but not a dict -> not served early
    api.clients = [ScriptedClient(RuntimeError("timeout"))]
    assert gw._fetch_from_nse_api("e", "ck") == ["legacy list"]


def test_fetch_from_nse_api_exception_with_no_cache_returns_none(api):
    api.clients = [ScriptedClient(RuntimeError("timeout"))]
    assert gw._fetch_from_nse_api("e", "ck") is None


def test_fetch_from_nse_api_bad_json_is_swallowed(api):
    api.clients = [ScriptedClient(Resp(200, json_raises=ValueError("not json")))]
    assert gw._fetch_from_nse_api("e", "ck") is None


def test_fetch_from_nse_api_client_construction_failure_is_swallowed(monkeypatch, api):
    def boom(force_new=False):
        raise RuntimeError("cannot build session")

    monkeypatch.setattr(gw, "_get_nse_client", boom)
    assert gw._fetch_from_nse_api("e", "ck") is None
