"""tests/test_refill_additional.py — coverage for api-gateway/refill_additional.py

The "Refill Additional Data" background job: a process-local job mirror (with stale-job detection),
a rate-limited single-retry GET helper, the fundamentals/technical/events -> data-feed row builder,
and the bounded-concurrency runner that writes rows through DataFeedStore.put_symbol.

No network, no threads (unless a test says so), no real data feed. Every test loads a FRESH copy of
the module (so the `_REFILL_JOB` mirror and the import-time env constants never leak between tests)
with `httpx`, `rate_limiter` and `data_feed` replaced by small fakes in sys.modules. The module's
`time` is a fake clock (the 429 back-off never really sleeps) and, by default, its
ThreadPoolExecutor / as_completed are swapped for a synchronous, in-order stand-in so the runner's
counters, progress messages and stop handling are deterministic. One class runs the real thread pool
to prove the concurrent path gives the same totals.

A separate class guards against drift: every name main.py imports from this module exists, every
data_feed / rate_limiter call the module makes matches the real signature.

Run from services/api-gateway:
    python3 -m pytest tests/test_refill_additional.py -v
"""
from __future__ import annotations

import ast
import importlib.util
import inspect
import logging
import os
import sys
import types
from concurrent.futures import Future
from datetime import timedelta

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
_MOD_PATH = os.path.join(_SERVICE, "refill_additional.py")

_ENV_KEYS = (
    "ANALYSIS_INTELLIGENCE_URL", "FUNDAMENTAL_URL", "TECHNICAL_URL", "EVENT_URL",
    "REFILL_TIMEOUT_SEC", "REFILL_CONCURRENCY", "REFILL_MAX_SYMBOLS", "REFILL_STALE_AFTER_SEC",
)

_AI = "https://analysis-intelligence-service.onrender.com"


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status=200, body=None, content=None):
        self.status_code = status
        self._body = body
        self.content = content if content is not None else (b"{}" if body is not None else b"")

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def ok(body):
    return FakeResp(200, body)


class FakeHttp:
    """Stand-in for the httpx module. `routes` maps url -> response, exception, or a list of them
    (consumed one per GET, the last one repeating); unknown urls answer 404."""

    def __init__(self):
        self.routes = {}
        self.gets = []              # (url, timeout)
        self.clients = []
        self.limits = []

    def module(self):
        http = self
        m = types.ModuleType("httpx")

        class Limits:
            def __init__(self, **kw):
                self.kw = kw
                http.limits.append(kw)

        class Client:
            def __init__(self, **kw):
                self.kw = kw
                self.closed = False
                http.clients.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.closed = True
                return False

            def get(self, url, timeout=None):
                http.gets.append((url, timeout))
                r = http.routes.get(url)
                if isinstance(r, list):
                    r = r.pop(0) if len(r) > 1 else r[0]
                if isinstance(r, Exception):
                    raise r
                return r if r is not None else FakeResp(404, {})

        m.Limits = Limits
        m.Client = Client
        return m

    def urls(self):
        return [u for u, _ in self.gets]


class FakeRateLimiter:
    def __init__(self):
        self.acquired = []
        self.timeouts = []
        self.acquire_raises = None
        self.timeout_raises = None
        self.timeout_factor = 2.0

    def module(self):
        rl = self
        m = types.ModuleType("rate_limiter")

        def acquire(provider, weight=1.0, max_wait=None, pipeline=None):
            if rl.acquire_raises is not None:
                raise rl.acquire_raises
            rl.acquired.append((provider, weight))
            return 0.0

        def suggested_timeout(base_timeout, provider, floor=1.0):
            if rl.timeout_raises is not None:
                raise rl.timeout_raises
            rl.timeouts.append((base_timeout, provider))
            return base_timeout * rl.timeout_factor

        m.acquire = acquire
        m.suggested_timeout = suggested_timeout
        return m


class FakeStore:
    def __init__(self):
        self.symbols = []
        self.list_raises = None
        self.put = []               # (symbol, row)
        self.put_raises = {}        # symbol -> exception
        self.jobs = []              # kwargs of every set_job
        self.job_raises = None

    def list_symbols(self):
        if self.list_raises is not None:
            raise self.list_raises
        return self.symbols

    def put_symbol(self, symbol, payload):
        if symbol in self.put_raises:
            raise self.put_raises[symbol]
        self.put.append((symbol, payload))

    def set_job(self, **kw):
        if self.job_raises is not None:
            raise self.job_raises
        self.jobs.append(kw)
        return dict(kw)


class FakeFeed:
    """Stand-in for the data_feed module (only the three helpers the refill job imports)."""

    def __init__(self):
        self.store = FakeStore()
        self.stop_answers = []      # consumed per data_feed_stop_requested() call; then False
        self.stop_checks = 0
        self.clears = 0

    def module(self):
        feed = self
        m = types.ModuleType("data_feed")

        def get_data_feed_store():
            return feed.store

        def data_feed_stop_requested():
            feed.stop_checks += 1
            return feed.stop_answers.pop(0) if feed.stop_answers else False

        def clear_data_feed_stop():
            feed.clears += 1

        m.get_data_feed_store = get_data_feed_store
        m.data_feed_stop_requested = data_feed_stop_requested
        m.clear_data_feed_stop = clear_data_feed_stop
        return m


class FakeClock:
    def __init__(self):
        self.now = 10_000.0
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)


class SyncExecutor:
    """Runs every submitted job immediately, in order — deterministic stand-in for the pool."""

    instances = []

    def __init__(self, max_workers=None):
        self.max_workers = max_workers
        self.futures = []
        SyncExecutor.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def submit(self, fn, *args, **kw):
        fut = _TrackedFuture()
        try:
            fut.set_result(fn(*args, **kw))
        except BaseException as e:      # noqa: BLE001 — mirrors Future semantics
            fut.set_exception(e)
        self.futures.append(fut)
        return fut


class _TrackedFuture(Future):
    def __init__(self):
        super().__init__()
        self.cancelled_calls = 0

    def cancel(self):
        self.cancelled_calls += 1
        return super().cancel()


def _in_order(futs):
    return iter(list(futs))


class Env:
    def __init__(self, monkeypatch):
        self.mp = monkeypatch
        self.http = FakeHttp()
        self.rl = FakeRateLimiter()
        self.feed = FakeFeed()
        self.clock = FakeClock()
        self.n = 0

    def load(self, sync=True, no_rl=False, no_feed=False, **env):
        for k, v in env.items():
            self.mp.setenv(k, v)
        self.mp.setitem(sys.modules, "httpx", self.http.module())
        self.mp.setitem(sys.modules, "rate_limiter", None if no_rl else self.rl.module())
        self.mp.setitem(sys.modules, "data_feed", None if no_feed else self.feed.module())
        self.n += 1
        spec = importlib.util.spec_from_file_location(f"refill_additional_under_test_{self.n}", _MOD_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.time = self.clock
        if sync:
            SyncExecutor.instances = []
            mod.ThreadPoolExecutor = SyncExecutor
            mod.as_completed = _in_order
        return mod

    # convenience ---------------------------------------------------------
    def serve(self, sym, fund=None, tech=None, events=None, mod=None):
        m = mod
        self.http.routes[f"{m.FUNDAMENTAL_URL}/analyze/{sym}?force=true"] = ok(fund) if fund is not None else FakeResp(404, {})
        self.http.routes[f"{m.TECHNICAL_URL}/analyze/{sym}?force=true"] = ok(tech) if tech is not None else FakeResp(404, {})
        self.http.routes[f"{m.EVENT_URL}/events/{sym}?force=true"] = ok(events) if events is not None else FakeResp(404, {})


@pytest.fixture
def env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return Env(monkeypatch)


@pytest.fixture
def m(env):
    return env.load()


# ── import-time constants ─────────────────────────────────────────────────────

class TestConstants:
    def test_defaults(self, m):
        assert m.FUNDAMENTAL_URL == f"{_AI}/fundamental"
        assert m.TECHNICAL_URL == f"{_AI}/technical"
        assert m.EVENT_URL == f"{_AI}/event"
        assert m.REQUEST_TIMEOUT == 25.0
        assert m.CONCURRENCY == 4
        assert m.MAX_SYMBOLS == 0
        assert m.STALE_AFTER_SEC == 300.0
        assert m.IST.utcoffset(None) == timedelta(hours=5, minutes=30)

    def test_base_url_override_flows_to_all_three(self, env):
        mod = env.load(ANALYSIS_INTELLIGENCE_URL="http://ai:9000///")
        assert mod.FUNDAMENTAL_URL == "http://ai:9000/fundamental"
        assert mod.TECHNICAL_URL == "http://ai:9000/technical"
        assert mod.EVENT_URL == "http://ai:9000/event"

    def test_individual_urls_win_and_lose_trailing_slash(self, env):
        mod = env.load(ANALYSIS_INTELLIGENCE_URL="http://ai", FUNDAMENTAL_URL="http://f/x/",
                       TECHNICAL_URL="http://t//", EVENT_URL="http://e")
        assert (mod.FUNDAMENTAL_URL, mod.TECHNICAL_URL, mod.EVENT_URL) == ("http://f/x", "http://t", "http://e")

    def test_numeric_overrides(self, env):
        mod = env.load(REFILL_TIMEOUT_SEC="7.5", REFILL_CONCURRENCY="9", REFILL_MAX_SYMBOLS="12",
                       REFILL_STALE_AFTER_SEC="60")
        assert (mod.REQUEST_TIMEOUT, mod.CONCURRENCY, mod.MAX_SYMBOLS, mod.STALE_AFTER_SEC) == (7.5, 9, 12, 60.0)

    def test_blank_max_symbols_means_full_universe(self, env):
        assert env.load(REFILL_MAX_SYMBOLS="").MAX_SYMBOLS == 0

    def test_initial_job_mirror(self, m):
        assert m._REFILL_JOB == {"status": "idle", "message": "Idle", "processed": 0, "total": 0,
                                 "ok_count": 0, "error_count": 0, "kind": "refill_additional"}

    def test_job_mirror_is_per_module_copy(self, env):
        a, b = env.load(), env.load()
        a._set_job(status="running")
        assert b._REFILL_JOB["status"] == "idle"


# ── get_refill_job ────────────────────────────────────────────────────────────

class TestGetRefillJob:
    def test_idle_snapshot(self, m):
        assert m.get_refill_job() == m._REFILL_JOB

    def test_returns_a_copy(self, m):
        job = m.get_refill_job()
        job["status"] = "tampered"
        assert m._REFILL_JOB["status"] == "idle"

    def test_fresh_running_job_stays_running(self, m, env):
        m._set_job(status="running", message="working")
        env.clock.now += 299
        job = m.get_refill_job()
        assert job["status"] == "running" and job["message"] == "working"

    def test_exactly_at_the_threshold_is_not_stalled(self, m, env):
        m._set_job(status="running", message="working")
        env.clock.now += 300
        assert m.get_refill_job()["status"] == "running"

    def test_stale_running_job_is_reported_stalled(self, m, env):
        m._set_job(status="running", message="working")
        env.clock.now += 301.9
        job = m.get_refill_job()
        assert job["status"] == "stalled"
        assert job["message"] == ("No progress for 301s — job likely died "
                                  "(dyno idle-kill/restart). Safe to start again.")

    def test_stall_is_reported_on_the_copy_only(self, m, env):
        m._set_job(status="running")
        env.clock.now += 1000
        m.get_refill_job()
        assert m._REFILL_JOB["status"] == "running"      # the real mirror is untouched

    def test_stale_threshold_is_env_driven(self, env):
        mod = env.load(REFILL_STALE_AFTER_SEC="10")
        mod._set_job(status="running")
        env.clock.now += 11
        assert mod.get_refill_job()["status"] == "stalled"

    @pytest.mark.parametrize("status", ["idle", "done", "error", "stopped", "stalled"])
    def test_only_running_jobs_can_stall(self, m, env, status):
        m._set_job(status=status, message="m")
        env.clock.now += 10_000
        assert m.get_refill_job()["status"] == status

    def test_running_without_a_heartbeat_is_left_alone(self, m, env):
        m._REFILL_JOB["status"] = "running"              # never went through _set_job
        env.clock.now += 10_000
        assert m.get_refill_job()["status"] == "running"

    def test_unusable_heartbeat_is_swallowed(self, m):
        m._REFILL_JOB.update(status="running", _updated_epoch="not-a-number")
        assert m.get_refill_job()["status"] == "running"


# ── _set_job ──────────────────────────────────────────────────────────────────

class TestSetJob:
    def test_updates_and_returns_a_copy(self, m):
        out = m._set_job(status="running", message="hi", processed=3, total=10, ok_count=2, error_count=1)
        assert out is not m._REFILL_JOB
        for k, v in dict(status="running", message="hi", processed=3, total=10, ok_count=2, error_count=1).items():
            assert m._REFILL_JOB[k] == v == out[k]
        out["status"] = "tampered"
        assert m._REFILL_JOB["status"] == "running"

    def test_extra_keys_are_kept(self, m):
        m._set_job(started_at="S", stop_requested=False)
        assert m._REFILL_JOB["started_at"] == "S" and m._REFILL_JOB["stop_requested"] is False

    def test_stamps_ist_time_and_epoch(self, m, env):
        m._set_job(status="running")
        assert m._REFILL_JOB["_updated_epoch"] == env.clock.now
        assert m._REFILL_JOB["updated_at"].endswith("+05:30")

    def test_untouched_keys_survive(self, m):
        m._set_job(total=50)
        m._set_job(message="later")
        assert m._REFILL_JOB["total"] == 50 and m._REFILL_JOB["message"] == "later"

    def test_mirrors_to_the_data_feed_job(self, m, env):
        m._set_job(status="running", message="go", processed=1, total=4, ok_count=1, error_count=0)
        (kw,) = env.feed.store.jobs
        assert kw == {"status": "running", "message": "go", "processed": 1, "total": 4, "ok_count": 1,
                      "error_count": 0, "kind": "refill_additional", "updated_at": m._REFILL_JOB["updated_at"]}

    def test_mirror_falls_back_to_current_values_for_unset_fields(self, m, env):
        m._set_job(status="running", message="go", total=9)
        m._set_job(message="just the message")
        kw = env.feed.store.jobs[-1]
        assert kw["status"] == "running" and kw["total"] == 9
        assert kw["message"] == "just the message" and kw["processed"] == 0

    def test_extra_keys_are_not_mirrored(self, m, env):
        m._set_job(started_at="S", stop_requested=True)
        kw = env.feed.store.jobs[-1]
        assert "started_at" not in kw and "stop_requested" not in kw

    def test_mirror_failure_is_swallowed(self, m, env, caplog):
        env.feed.store.job_raises = RuntimeError("store down")
        with caplog.at_level(logging.DEBUG, logger="refill-additional"):
            out = m._set_job(status="running")
        assert out["status"] == "running" and m._REFILL_JOB["status"] == "running"
        assert any("refill job mirror" in r.getMessage() for r in caplog.records)

    def test_data_feed_missing_is_swallowed(self, env):
        mod = env.load(no_feed=True)
        assert mod._set_job(status="done")["status"] == "done"


# ── _norm ─────────────────────────────────────────────────────────────────────

class TestNorm:
    @pytest.mark.parametrize("raw, expected", [
        ("tcs", "TCS"), (" tcs.ns ", "TCS"), ("infy.bo", "INFY"), ("Reliance.NS", "RELIANCE"),
        ("", ""), (None, ""), ("   ", ""), (".NS", ""), (".bo", ""), (123, "123"),
        ("M&M", "M&M"), ("BAJAJ-AUTO.NS", "BAJAJ-AUTO"),
    ])
    def test_cases(self, m, raw, expected):
        assert m._norm(raw) == expected


# ── _get_json ─────────────────────────────────────────────────────────────────

URL = "http://x/analyze/AAA?force=true"


def _client(env, mod):
    return env.http.module().Client()


class TestGetJson:
    def test_success(self, m, env):
        env.http.routes[URL] = ok({"a": 1})
        assert m._get_json(_client(env, m), URL) == {"a": 1}
        assert env.http.urls() == [URL]

    def test_rate_limiter_slot_and_widened_timeout(self, m, env):
        env.http.routes[URL] = ok({})
        env.rl.timeout_factor = 3.0
        m._get_json(_client(env, m), URL)
        assert env.rl.acquired == [("analysis", 1)]
        assert env.rl.timeouts == [(25.0, "analysis")]
        assert env.http.gets == [(URL, 75.0)]

    def test_rate_limiter_uses_the_env_timeout(self, env):
        mod = env.load(REFILL_TIMEOUT_SEC="4")
        env.http.routes[URL] = ok({})
        mod._get_json(_client(env, mod), URL)
        assert env.rl.timeouts == [(4.0, "analysis")]

    def test_rate_limiter_missing_falls_back_to_base_timeout(self, env):
        mod = env.load(no_rl=True)
        env.http.routes[URL] = ok({"a": 1})
        assert mod._get_json(_client(env, mod), URL) == {"a": 1}
        assert env.http.gets == [(URL, 25.0)]

    def test_acquire_failure_falls_back_and_still_requests(self, m, env):
        env.rl.acquire_raises = RuntimeError("limiter broke")
        env.http.routes[URL] = ok({"a": 1})
        assert m._get_json(_client(env, m), URL) == {"a": 1}
        assert env.http.gets == [(URL, 25.0)]

    def test_suggested_timeout_failure_falls_back_and_still_requests(self, m, env):
        env.rl.timeout_raises = RuntimeError("no timeout for you")
        env.http.routes[URL] = ok({"a": 1})
        assert m._get_json(_client(env, m), URL) == {"a": 1}
        assert env.http.gets == [(URL, 25.0)]

    def test_429_then_200_retries_once_after_five_seconds(self, m, env):
        env.http.routes[URL] = [FakeResp(429, {}), ok({"a": 1})]
        assert m._get_json(_client(env, m), URL) == {"a": 1}
        assert env.http.urls() == [URL, URL]
        assert env.clock.slept == [5]
        assert env.rl.acquired == [("analysis", 1)]           # one slot per call, not per attempt

    def test_second_429_gives_up(self, m, env):
        env.http.routes[URL] = [FakeResp(429, {})]
        assert m._get_json(_client(env, m), URL) is None
        assert env.http.urls() == [URL, URL] and env.clock.slept == [5]

    @pytest.mark.parametrize("status", [500, 502, 503, 404, 401, 204, 301])
    def test_other_statuses_are_not_retried(self, m, env, status):
        env.http.routes[URL] = FakeResp(status, {"a": 1})
        assert m._get_json(_client(env, m), URL) is None
        assert env.http.urls() == [URL] and env.clock.slept == []

    @pytest.mark.parametrize("resp", [FakeResp(200, {"a": 1}, content=b""), FakeResp(200, None)])
    def test_empty_body_is_none(self, m, env, resp):
        env.http.routes[URL] = resp
        assert m._get_json(_client(env, m), URL) is None

    @pytest.mark.parametrize("body", [[1, 2], "text", 5, None, True])
    def test_non_dict_json_is_none(self, m, env, body):
        env.http.routes[URL] = FakeResp(200, body, content=b"x")
        assert m._get_json(_client(env, m), URL) is None

    def test_empty_dict_is_returned_as_a_dict(self, m, env):
        env.http.routes[URL] = FakeResp(200, {}, content=b"{}")
        assert m._get_json(_client(env, m), URL) == {}

    def test_json_decode_error_is_none(self, m, env):
        env.http.routes[URL] = FakeResp(200, ValueError("bad json"), content=b"<html>")
        assert m._get_json(_client(env, m), URL) is None

    def test_request_exception_is_none_and_logged(self, m, env, caplog):
        env.http.routes[URL] = RuntimeError("connect boom")
        with caplog.at_level(logging.DEBUG, logger="refill-additional"):
            assert m._get_json(_client(env, m), URL) is None
        assert any(URL in r.getMessage() and "connect boom" in r.getMessage() for r in caplog.records)

    def test_exception_on_the_retry_is_none(self, m, env):
        env.http.routes[URL] = [FakeResp(429, {}), RuntimeError("second try boom")]
        assert m._get_json(_client(env, m), URL) is None
        assert env.clock.slept == [5]


# ── _build_payload ────────────────────────────────────────────────────────────

_FUND_KEYS = ("pe_ratio", "roe", "roce", "debt_to_equity", "revenue_growth", "market_cap", "sector",
              "industry", "quality_score", "fundamental_score", "promoter_holding", "eps", "book_value",
              "dividend_yield", "forward_pe", "earnings_growth")
_TECH_KEYS = ("technical_score", "rsi", "trend_strength", "volume_surge", "support", "resistance", "close")
_EVENT_KEYS = ("next_earnings_date", "earnings_surprise", "event_summary", "has_positive_catalyst",
               "recent_event_score")


class TestBuildPayload:
    def test_nothing_known_is_just_symbol_and_source(self, m):
        assert m._build_payload("AAA", None, None, None) == {"symbol": "AAA", "source": "refill_additional"}

    def test_empty_dicts_are_the_same_as_none(self, m):
        assert m._build_payload("AAA", {}, {}, {}) == {"symbol": "AAA", "source": "refill_additional"}

    @pytest.mark.parametrize("key", _FUND_KEYS)
    def test_every_fundamental_key_is_copied_with_a_seed_flag(self, m, key):
        row = m._build_payload("AAA", {key: "V"}, None, None)
        assert row[key] == "V" and row[f"{key}_seed"] is False

    @pytest.mark.parametrize("key", _FUND_KEYS)
    def test_every_fundamental_key_falls_back_to_metrics(self, m, key):
        row = m._build_payload("AAA", {"metrics": {key: "M"}}, None, None)
        assert row[key] == "M" and row[f"{key}_seed"] is False

    def test_top_level_beats_metrics(self, m):
        row = m._build_payload("AAA", {"pe_ratio": 10, "metrics": {"pe_ratio": 99}}, None, None)
        assert row["pe_ratio"] == 10

    def test_none_top_level_uses_metrics(self, m):
        row = m._build_payload("AAA", {"pe_ratio": None, "metrics": {"pe_ratio": 99}}, None, None)
        assert row["pe_ratio"] == 99

    @pytest.mark.parametrize("val", [0, 0.0, "", False])
    def test_falsy_but_present_values_are_kept(self, m, val):
        row = m._build_payload("AAA", {"roe": val}, None, None)
        assert row["roe"] == val and row["roe_seed"] is False

    def test_none_everywhere_writes_no_key_and_no_seed_flag(self, m):
        row = m._build_payload("AAA", {"roe": None, "metrics": {"roe": None}}, None, None)
        assert "roe" not in row and "roe_seed" not in row

    @pytest.mark.parametrize("metrics", [None, "text", [1, 2], 5])
    def test_non_dict_metrics_is_ignored(self, m, metrics):
        row = m._build_payload("AAA", {"pe_ratio": 12, "metrics": metrics}, None, None)
        assert row == {"symbol": "AAA", "source": "refill_additional", "pe_ratio": 12, "pe_ratio_seed": False}

    def test_unknown_fundamental_keys_are_dropped(self, m):
        row = m._build_payload("AAA", {"mystery": 1, "metrics": {"mystery2": 2}}, None, None)
        assert row == {"symbol": "AAA", "source": "refill_additional"}

    def test_fundamental_summary(self, m):
        assert m._build_payload("A", {"summary": "Solid"}, None, None)["fundamental_summary"] == "Solid"
        assert "fundamental_summary" not in m._build_payload("A", {"summary": ""}, None, None)
        assert "fundamental_summary" not in m._build_payload("A", {"summary": None}, None, None)

    @pytest.mark.parametrize("key", _TECH_KEYS)
    def test_every_technical_key_is_copied_without_a_seed_flag(self, m, key):
        row = m._build_payload("AAA", None, {key: "T"}, None)
        assert row[key] == "T" and f"{key}_seed" not in row

    @pytest.mark.parametrize("val", [0, 0.0, "", False])
    def test_falsy_technical_values_are_kept(self, m, val):
        assert m._build_payload("AAA", None, {"rsi": val}, None)["rsi"] == val

    def test_none_technical_values_and_unknown_keys_are_dropped(self, m):
        row = m._build_payload("AAA", None, {"rsi": None, "macd": 1}, None)
        assert row == {"symbol": "AAA", "source": "refill_additional"}

    @pytest.mark.parametrize("key", _EVENT_KEYS)
    def test_every_event_key_is_copied(self, m, key):
        assert m._build_payload("AAA", None, None, {key: "E"})[key] == "E"

    @pytest.mark.parametrize("val", [0, 0.0, "", False])
    def test_falsy_event_values_are_kept(self, m, val):
        assert m._build_payload("AAA", None, None, {"recent_event_score": val})["recent_event_score"] == val

    def test_none_event_values_and_unknown_keys_are_dropped(self, m):
        row = m._build_payload("AAA", None, None, {"next_earnings_date": None, "other": 1})
        assert row == {"symbol": "AAA", "source": "refill_additional"}

    def test_event_summary_falls_back_to_summary(self, m):
        assert m._build_payload("A", None, None, {"summary": "S"})["event_summary"] == "S"

    def test_event_summary_beats_summary(self, m):
        row = m._build_payload("A", None, None, {"event_summary": "ES", "summary": "S"})
        assert row["event_summary"] == "ES"

    def test_none_event_summary_lets_summary_through(self, m):
        row = m._build_payload("A", None, None, {"event_summary": None, "summary": "S"})
        assert row["event_summary"] == "S"

    def test_empty_event_summary_string_is_kept_over_summary(self, m):
        row = m._build_payload("A", None, None, {"event_summary": "", "summary": "S"})
        assert row["event_summary"] == ""

    def test_falsy_summary_is_not_used(self, m):
        assert "event_summary" not in m._build_payload("A", None, None, {"summary": ""})

    def test_all_three_sources_combine(self, m):
        row = m._build_payload(
            "AAA",
            {"pe_ratio": 20, "summary": "F", "metrics": {"roe": 15}},
            {"rsi": 55, "close": 101.5},
            {"next_earnings_date": "2026-11-01", "summary": "E"},
        )
        assert row == {
            "symbol": "AAA", "source": "refill_additional",
            "pe_ratio": 20, "pe_ratio_seed": False, "roe": 15, "roe_seed": False,
            "fundamental_summary": "F", "rsi": 55, "close": 101.5,
            "next_earnings_date": "2026-11-01", "event_summary": "E",
        }

    def test_inputs_are_not_mutated(self, m):
        fund = {"pe_ratio": 20, "metrics": {"roe": 15}}
        tech = {"rsi": 55}
        events = {"summary": "E"}
        m._build_payload("AAA", fund, tech, events)
        assert fund == {"pe_ratio": 20, "metrics": {"roe": 15}}
        assert tech == {"rsi": 55} and events == {"summary": "E"}

    def test_a_row_with_data_is_longer_than_two_keys(self, m):
        # the runner treats len(row) <= 2 (symbol + source only) as "nothing found"
        assert len(m._build_payload("A", None, None, None)) == 2
        assert len(m._build_payload("A", None, {"rsi": 1}, None)) == 3


# ── _hydrate_one ──────────────────────────────────────────────────────────────

class TestHydrateOne:
    def test_three_urls_in_order_with_force(self, m, env):
        m._hydrate_one(_client(env, m), "AAA")
        assert env.http.urls() == [
            f"{m.FUNDAMENTAL_URL}/analyze/AAA?force=true",
            f"{m.TECHNICAL_URL}/analyze/AAA?force=true",
            f"{m.EVENT_URL}/events/AAA?force=true",
        ]

    def test_returns_symbol_and_built_row(self, m, env):
        env.serve("AAA", fund={"pe_ratio": 9}, tech={"rsi": 40}, events={"summary": "E"}, mod=m)
        sym, row = m._hydrate_one(_client(env, m), "AAA")
        assert sym == "AAA"
        assert row["pe_ratio"] == 9 and row["rsi"] == 40 and row["event_summary"] == "E"
        assert row["symbol"] == "AAA" and row["source"] == "refill_additional"

    def test_all_upstreams_down_gives_an_empty_row(self, m, env):
        _, row = m._hydrate_one(_client(env, m), "AAA")
        assert row == {"symbol": "AAA", "source": "refill_additional"}

    def test_one_failing_upstream_does_not_block_the_others(self, m, env):
        env.serve("AAA", tech={"rsi": 40}, mod=m)
        env.http.routes[f"{m.FUNDAMENTAL_URL}/analyze/AAA?force=true"] = RuntimeError("fund down")
        _, row = m._hydrate_one(_client(env, m), "AAA")
        assert row["rsi"] == 40 and "pe_ratio" not in row


# ── run_refill_additional ─────────────────────────────────────────────────────

def _statuses(env):
    return [j["status"] for j in env.feed.store.jobs]


class TestRunGuards:
    def test_clears_the_stop_flag_first(self, m, env):
        m.run_refill_additional(["AAA"])
        assert env.feed.clears == 1

    @pytest.mark.parametrize("given", [None, []])
    def test_falls_back_to_the_store_universe(self, m, env, given):
        env.feed.store.symbols = ["TCS", "INFY"]
        env.serve("TCS", tech={"rsi": 1}, mod=m)
        env.serve("INFY", tech={"rsi": 2}, mod=m)
        out = m.run_refill_additional(given)
        assert out["total"] == 2 and out["status"] == "done"
        assert [s for s, _ in env.feed.store.put] == ["TCS", "INFY"]

    @pytest.mark.parametrize("universe", [[], None])
    def test_no_symbols_anywhere_is_an_error_job(self, m, env, universe):
        env.feed.store.symbols = universe
        out = m.run_refill_additional(None)
        assert out["status"] == "error" and out["message"] == "No symbols to refill"
        assert out["processed"] == 0 and out["total"] == 0
        assert env.http.clients == []                      # never opened a client
        assert env.feed.clears == 1

    def test_universe_lookup_failure_is_an_error_job(self, m, env):
        env.feed.store.list_raises = RuntimeError("store boom")
        out = m.run_refill_additional([])
        assert out["status"] == "error" and out["message"] == "No symbols to refill"

    def test_the_error_job_is_mirrored(self, m, env):
        m.run_refill_additional([])
        assert _statuses(env) == ["error"]

    def test_explicit_symbols_skip_the_store_lookup(self, m, env):
        env.feed.store.list_raises = RuntimeError("must not be called")
        assert m.run_refill_additional(["AAA"])["status"] == "done"


class TestRunSymbolPrep:
    def test_normalised_deduped_in_first_seen_order(self, m, env):
        m.run_refill_additional([" tcs.ns", "TCS", "infy", "", None, "INFY.BO", "wipro"])
        assert [u.split("/analyze/")[1].split("?")[0] for u in env.http.urls() if "/fundamental/" in u] == \
               ["TCS", "INFY", "WIPRO"]

    def test_max_symbols_caps_after_dedupe(self, env):
        mod = env.load(REFILL_MAX_SYMBOLS="2")
        out = mod.run_refill_additional(["A", "a", "B", "C", "D"])
        assert out["total"] == 2
        assert env.feed.store.jobs[0]["total"] == 2

    def test_zero_max_symbols_means_no_cap(self, m, env):
        assert m.run_refill_additional([f"S{i}" for i in range(30)])["total"] == 30

    def test_max_symbols_larger_than_universe(self, env):
        mod = env.load(REFILL_MAX_SYMBOLS="100")
        assert mod.run_refill_additional(["A", "B"])["total"] == 2


class TestRunSetup:
    def test_initial_running_job(self, m, env):
        m._REFILL_JOB.update(ok_count=7, error_count=3, processed=9)   # leftovers from a previous run
        m.run_refill_additional(["A", "B"])
        first = env.feed.store.jobs[0]
        assert first["status"] == "running"
        assert first["message"] == "Refill Additional Data: 0/2 (concurrency=4)…"
        assert (first["processed"], first["total"], first["ok_count"], first["error_count"]) == (0, 2, 0, 0)

    def test_started_at_and_stop_flag_live_on_the_mirror(self, m, env):
        m.run_refill_additional(["A"])
        assert m._REFILL_JOB["started_at"].endswith("+05:30")
        assert m._REFILL_JOB["stop_requested"] is False

    def test_client_and_limits_configuration(self, m, env):
        m.run_refill_additional(["A"])
        (client,) = env.http.clients
        assert client.kw["timeout"] == 25.0 and client.kw["follow_redirects"] is True
        assert client.kw["limits"].kw == {"max_connections": 8, "max_keepalive_connections": 4}
        assert env.http.limits == [{"max_connections": 8, "max_keepalive_connections": 4}]
        assert client.closed is True

    def test_pool_size_follows_concurrency(self, env):
        mod = env.load(REFILL_CONCURRENCY="6")
        mod.run_refill_additional(["A"])
        assert SyncExecutor.instances[0].max_workers == 6
        assert env.http.limits == [{"max_connections": 12, "max_keepalive_connections": 6}]

    @pytest.mark.parametrize("raw", ["0", "-3"])
    def test_pool_never_has_fewer_than_one_worker(self, env, raw):
        mod = env.load(REFILL_CONCURRENCY=raw)
        mod.run_refill_additional(["A"])
        assert SyncExecutor.instances[0].max_workers == 1

    def test_message_reports_configured_concurrency(self, env):
        mod = env.load(REFILL_CONCURRENCY="7")
        mod.run_refill_additional(["A"])
        assert env.feed.store.jobs[0]["message"] == "Refill Additional Data: 0/1 (concurrency=7)…"


class TestRunOutcomes:
    def test_all_ok(self, m, env):
        for s in ("AAA", "BBB"):
            env.serve(s, fund={"pe_ratio": 10}, tech={"rsi": 50}, mod=m)
        out = m.run_refill_additional(["AAA", "BBB"])
        assert out["status"] == "done"
        assert out["message"] == "Refill complete: 2/2 ok, 0 errors"
        assert (out["processed"], out["total"], out["ok_count"], out["error_count"]) == (2, 2, 2, 0)
        assert [s for s, _ in env.feed.store.put] == ["AAA", "BBB"]
        assert env.feed.store.put[0][1]["pe_ratio"] == 10

    def test_symbol_with_no_data_counts_as_an_error_and_is_not_stored(self, m, env):
        env.serve("AAA", tech={"rsi": 50}, mod=m)
        out = m.run_refill_additional(["AAA", "GHOST"])
        assert (out["ok_count"], out["error_count"]) == (1, 1)
        assert out["message"] == "Refill complete: 1/2 ok, 1 errors"
        assert [s for s, _ in env.feed.store.put] == ["AAA"]

    def test_partial_data_is_enough(self, m, env):
        env.serve("AAA", events={"next_earnings_date": "2026-11-01"}, mod=m)
        out = m.run_refill_additional(["AAA"])
        assert out["ok_count"] == 1 and out["error_count"] == 0

    def test_store_write_failure_is_isolated(self, m, env, caplog):
        for s in ("AAA", "BBB", "CCC"):
            env.serve(s, tech={"rsi": 1}, mod=m)
        env.feed.store.put_raises["BBB"] = RuntimeError("write boom")
        with caplog.at_level(logging.WARNING, logger="refill-additional"):
            out = m.run_refill_additional(["AAA", "BBB", "CCC"])
        assert (out["ok_count"], out["error_count"], out["processed"]) == (2, 1, 3)
        assert [s for s, _ in env.feed.store.put] == ["AAA", "CCC"]
        assert any("refill BBB" in r.getMessage() and "write boom" in r.getMessage() for r in caplog.records)

    def test_hydrate_exception_is_isolated(self, m, env, monkeypatch, caplog):
        real = m._hydrate_one

        def flaky(client, sym):
            if sym == "BAD":
                raise RuntimeError("hydrate boom")
            return real(client, sym)

        monkeypatch.setattr(m, "_hydrate_one", flaky)
        env.serve("OK", tech={"rsi": 1}, mod=m)
        with caplog.at_level(logging.WARNING, logger="refill-additional"):
            out = m.run_refill_additional(["BAD", "OK"])
        assert (out["ok_count"], out["error_count"]) == (1, 1)
        assert any("refill BAD" in r.getMessage() for r in caplog.records)

    def test_the_stored_symbol_is_the_normalised_one(self, m, env):
        env.serve("TCS", tech={"rsi": 1}, mod=m)
        m.run_refill_additional(["tcs.ns"])
        assert env.feed.store.put[0][0] == "TCS"

    def test_final_state_is_visible_through_get_refill_job(self, m, env):
        env.serve("AAA", tech={"rsi": 1}, mod=m)
        m.run_refill_additional(["AAA"])
        job = m.get_refill_job()
        assert job["status"] == "done" and job["ok_count"] == 1 and job["processed"] == 1


class TestRunProgress:
    def _run7(self, m, env):
        for i in range(7):
            env.serve(f"S{i}", tech={"rsi": i}, mod=m)
        return m.run_refill_additional([f"S{i}" for i in range(7)])

    def test_progress_every_third_symbol_and_at_the_end(self, m, env):
        self._run7(m, env)
        jobs = env.feed.store.jobs
        assert _statuses(env) == ["running", "running", "running", "running", "done"]
        assert [j["processed"] for j in jobs] == [0, 3, 6, 7, 7]
        assert jobs[1]["message"] == "Refill Additional Data: 3/7 (ok=3 err=0)"
        assert jobs[3]["message"] == "Refill Additional Data: 7/7 (ok=7 err=0)"

    def test_progress_carries_the_running_counters(self, m, env):
        env.serve("A", tech={"rsi": 1}, mod=m)
        env.serve("C", tech={"rsi": 1}, mod=m)
        m.run_refill_additional(["A", "GHOST", "C"])
        progress = env.feed.store.jobs[1]
        assert progress["message"] == "Refill Additional Data: 3/3 (ok=2 err=1)"
        assert (progress["ok_count"], progress["error_count"]) == (2, 1)

    def test_single_symbol_still_reports_a_final_progress_tick(self, m, env):
        env.serve("A", tech={"rsi": 1}, mod=m)
        m.run_refill_additional(["A"])
        assert _statuses(env) == ["running", "running", "done"]

    def test_each_progress_tick_refreshes_the_heartbeat(self, m, env):
        env.serve("A", tech={"rsi": 1}, mod=m)
        m.run_refill_additional(["A"])
        assert m._REFILL_JOB["_updated_epoch"] == env.clock.now


class TestRunStop:
    def _serve(self, m, env, n):
        syms = [f"S{i}" for i in range(n)]
        for s in syms:
            env.serve(s, tech={"rsi": 1}, mod=m)
        return syms

    def test_stop_before_anything_is_processed(self, m, env):
        syms = self._serve(m, env, 4)
        env.feed.stop_answers = [True]
        out = m.run_refill_additional(syms)
        assert out["status"] == "stopped" and out["message"] == "Stopped at 0/4"
        assert (out["processed"], out["ok_count"], out["error_count"]) == (0, 0, 0)
        assert env.feed.store.put == []

    def test_stop_midway_keeps_what_was_done(self, m, env):
        syms = self._serve(m, env, 5)
        env.feed.stop_answers = [False, False, True]
        out = m.run_refill_additional(syms)
        assert out["message"] == "Stopped at 2/5"
        assert (out["processed"], out["ok_count"], out["error_count"]) == (2, 2, 0)
        assert [s for s, _ in env.feed.store.put] == ["S0", "S1"]

    def test_stop_cancels_every_pending_future(self, m, env):
        syms = self._serve(m, env, 3)
        env.feed.stop_answers = [True]
        m.run_refill_additional(syms)
        assert [f.cancelled_calls for f in SyncExecutor.instances[0].futures] == [1, 1, 1]

    def test_stop_never_sets_done(self, m, env):
        syms = self._serve(m, env, 3)
        env.feed.stop_answers = [False, True]
        m.run_refill_additional(syms)
        assert "done" not in _statuses(env)
        assert _statuses(env)[-1] == "stopped"
        assert m.get_refill_job()["status"] == "stopped"

    def test_stop_is_checked_once_per_completed_future(self, m, env):
        syms = self._serve(m, env, 4)
        m.run_refill_additional(syms)
        assert env.feed.stop_checks == 4

    def test_stop_counts_errors_seen_so_far(self, m, env):
        env.serve("A", tech={"rsi": 1}, mod=m)
        env.feed.stop_answers = [False, False, True]
        out = m.run_refill_additional(["A", "GHOST", "C"])
        assert out["message"] == "Stopped at 2/3"
        assert (out["ok_count"], out["error_count"]) == (1, 1)


class TestRunWithRealThreadPool:
    def test_concurrent_run_matches_the_serial_totals(self, env):
        mod = env.load(sync=False, REFILL_CONCURRENCY="3")
        syms = [f"S{i}" for i in range(20)]
        for s in syms[:15]:
            env.serve(s, tech={"rsi": 1}, mod=mod)
        out = mod.run_refill_additional(syms)
        assert out["status"] == "done"
        assert (out["processed"], out["total"], out["ok_count"], out["error_count"]) == (20, 20, 15, 5)
        assert sorted(s for s, _ in env.feed.store.put) == sorted(syms[:15])
        assert out["message"] == "Refill complete: 15/20 ok, 5 errors"

    def test_concurrent_stop_returns_a_stopped_job(self, env):
        mod = env.load(sync=False, REFILL_CONCURRENCY="2")
        syms = [f"S{i}" for i in range(6)]
        for s in syms:
            env.serve(s, tech={"rsi": 1}, mod=mod)
        env.feed.stop_answers = [False, True]
        out = mod.run_refill_additional(syms)
        assert out["status"] == "stopped" and out["message"] == "Stopped at 1/6"
        assert env.http.clients[0].closed is True


# ── drift guards against the collaborators ───────────────────────────────────

def _load_real(name, filename):
    path = os.path.join(_SERVICE, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # @dataclass needs the module registered while it executes
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(name, None)
    return mod


def _ast(filename):
    with open(os.path.join(_SERVICE, filename), encoding="utf-8") as fh:
        return ast.parse(fh.read())


class TestDrift:
    def test_names_main_imports_from_this_module_exist(self):
        tree = _ast("main.py")
        wanted = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "refill_additional":
                wanted |= {a.name for a in node.names}
        assert {"get_refill_job", "run_refill_additional", "_set_job"} <= wanted
        src = _ast("refill_additional.py")
        defined = {n.name for n in src.body if isinstance(n, ast.FunctionDef)}
        assert wanted <= defined

    def test_data_feed_helpers_the_job_imports_exist(self):
        feed = _ast("data_feed.py")
        top = {n.name for n in feed.body if isinstance(n, ast.FunctionDef)}
        assert {"get_data_feed_store", "data_feed_stop_requested", "clear_data_feed_stop"} <= top

    def test_store_methods_match_how_the_job_calls_them(self):
        feed = _ast("data_feed.py")
        methods = {}
        for cls in (n for n in feed.body if isinstance(n, ast.ClassDef) and n.name == "DataFeedStore"):
            for fn in cls.body:
                if isinstance(fn, ast.FunctionDef):
                    methods[fn.name] = fn
        assert {"put_symbol", "list_symbols", "set_job"} <= set(methods)
        put_args = [a.arg for a in methods["put_symbol"].args.args]
        assert put_args[:3] == ["self", "symbol", "payload"]          # put_symbol(sym, row)
        assert methods["set_job"].args.kwarg is not None              # set_job(**kwargs) takes every mirror field
        assert [a.arg for a in methods["list_symbols"].args.args] == ["self"]

    def test_rate_limiter_signatures_match_the_calls(self):
        rl = _load_real("rate_limiter_for_refill_drift", "rate_limiter.py")
        acq = inspect.signature(rl.acquire)
        sug = inspect.signature(rl.suggested_timeout)
        acq.bind("analysis", weight=1)
        sug.bind(25.0, "analysis")

    def test_the_urls_the_job_calls_exist_on_the_upstream_services(self):
        # the three upstream routes are hit as <base>/analyze/<sym>, <base>/analyze/<sym>, <base>/events/<sym>
        root = os.path.dirname(os.path.dirname(_SERVICE))
        base = os.path.join(root, "services", "analysis-intelligence-service")
        if not os.path.isdir(base):
            pytest.skip("analysis-intelligence-service not present")
        blob = ""
        for dirpath, _dirs, files in os.walk(base):
            if "tests" in dirpath.split(os.sep) or "__pycache__" in dirpath:
                continue
            for f in files:
                if f.endswith(".py"):
                    with open(os.path.join(dirpath, f), encoding="utf-8", errors="ignore") as fh:
                        blob += fh.read()
        assert "/analyze/{" in blob and "/events/{" in blob
