"""
tests/test_rate_limiter.py — api-gateway/rate_limiter.py (100%).

The gateway copy has diverged from the rate_limiter.py in analysis-intelligence /
market-data / notification-scheduler (fail-fast buckets, interactive reserve, pipeline scopes,
symbol_aliases bridge, hard-timeout pool), so these tests are written for this file.

Hermetic: no network, no yfinance, no real sleeping, no real threads for rename discovery.
  * time            -> FakeClock injected as `rl.time` (sleep() just advances the clock)
  * yfinance        -> a fake `yfinance` module built per test (patch_yfinance monkeypatches it)
  * symbol_aliases  -> a fake module in sys.modules (or None to model "not shipped here")
  * threads         -> `rl.threading` replaced by a namespace whose Thread runs the target inline
  * global state    -> buckets, patch flag, discovery set, pipeline label reset every test

Regression pinned here: the patched yf.download() used to raise IndexError for an empty
`tickers` value (it indexed symbols[0] on an empty list after the real call returned).

    cd services/api-gateway
    python -m pytest tests/test_rate_limiter.py -v --cov=rate_limiter --cov-report=term-missing
"""
from __future__ import annotations

import os
import sys
import threading
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import rate_limiter as rl


# ─────────────────────────────── fakes ───────────────────────────────

class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now
        self.sleeps = []
        self.sleep_raises = None

    def time(self):
        return self.now

    def sleep(self, s):
        if self.sleep_raises:
            raise self.sleep_raises
        self.sleeps.append(s)
        self.now += s


class LogSpy:
    def __init__(self):
        self.rec = []

    def _mk(self, level):
        return lambda msg, *a, **k: self.rec.append((level, msg % a if a else msg))

    def __getattr__(self, name):
        if name in ("debug", "info", "warning", "error", "exception"):
            return self._mk(name)
        raise AttributeError(name)

    def text(self, level=None):
        return "\n".join(m for lv, m in self.rec if level in (None, lv))


class FakeAliases:
    def __init__(self):
        self.failures = []
        self.cleared = []
        self.delisted = set()
        self.high_price = set()
        self.streak = 1
        self.discover_result = None
        self.discover_calls = []
        self.raise_on = set()

    def _maybe(self, name):
        if name in self.raise_on:
            raise RuntimeError(f"{name} exploded")

    def is_learned_delisted(self, base):
        self._maybe("is_learned_delisted")
        return base in self.delisted

    def is_known_high_price(self, base):
        self._maybe("is_known_high_price")
        return base in self.high_price

    def record_resolution_failure(self, base):
        self._maybe("record_resolution_failure")
        self.failures.append(base)
        return self.streak

    def clear_resolution_failures(self, base):
        self._maybe("clear_resolution_failures")
        self.cleared.append(base)

    def try_discover_rename(self, base, timeout=None):
        self._maybe("try_discover_rename")
        self.discover_calls.append((base, timeout))
        return self.discover_result


class FakeThread:
    made = []
    start_raises = None
    ctor_raises = None

    def __init__(self, target=None, name=None, daemon=None):
        if FakeThread.ctor_raises:
            raise FakeThread.ctor_raises
        self.target, self.name, self.daemon = target, name, daemon
        FakeThread.made.append(self)

    def start(self):
        if FakeThread.start_raises:
            raise FakeThread.start_raises
        self.target()


_RL_ENV = tuple(f"RL_{p.upper()}_{k}" for p in ("yfinance", "indianapi", "nse", "market_data",
                                                 "analysis", "brandnew") for k in ("RPS", "BURST"))


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    for k in _RL_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(rl, "_buckets", {})
    monkeypatch.setattr(rl, "_yf_patched", False)
    monkeypatch.setattr(rl, "_DISCOVERY_TRIED", set())
    monkeypatch.setattr(rl, "_DISCOVERY_SLOT", threading.BoundedSemaphore(1))
    monkeypatch.setattr(rl._pipeline_tls, "name", "", raising=False)
    monkeypatch.setattr(rl, "SKIP_HIGH_PRICE", False)
    monkeypatch.setattr(rl, "RENAME_DISCOVERY", True)
    monkeypatch.setattr(rl, "DISCOVERY_AT_STREAK", 2)
    monkeypatch.setattr(rl, "DISCOVERY_TIMEOUT", 8.0)
    monkeypatch.setattr(rl, "MAX_WAIT_DEFAULT", 5.0)
    monkeypatch.setattr(rl, "MIN_WAIT_FLOOR", 0.5)
    monkeypatch.setattr(rl, "INTERACTIVE_RESERVE_FRACTION", 0.34)
    monkeypatch.setattr(rl, "BACKGROUND_PIPELINES", {"scan", "surprise", "ipo", "hotpicks"})
    FakeThread.made = []
    FakeThread.start_raises = None
    FakeThread.ctor_raises = None
    yield


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(rl, "time", c)
    return c


@pytest.fixture
def log(monkeypatch):
    spy = LogSpy()
    monkeypatch.setattr(rl, "logger", spy)
    return spy


@pytest.fixture
def sa(monkeypatch):
    fake = FakeAliases()
    monkeypatch.setitem(sys.modules, "symbol_aliases", fake)
    return fake


@pytest.fixture
def inline_threads(monkeypatch):
    monkeypatch.setattr(rl, "threading", types.SimpleNamespace(Thread=FakeThread))
    return FakeThread


def bucket(clock, rps=1.0, capacity=2.0, tokens=None):
    b = rl._Bucket(rps=rps, capacity=capacity)
    b.updated = clock.now
    if tokens is not None:
        b.tokens = tokens
    return b


# ─────────────────────── pipeline label / background ───────────────────────

class TestPipeline:
    def test_default_is_empty(self):
        assert rl.current_pipeline() == ""

    def test_scope_sets_and_restores(self):
        with rl.pipeline_scope("hotpicks"):
            assert rl.current_pipeline() == "hotpicks"
        assert rl.current_pipeline() == ""

    def test_label_is_normalised(self):
        with rl.pipeline_scope("  HotPicks "):
            assert rl.current_pipeline() == "hotpicks"

    def test_none_name_is_empty_label(self):
        with rl.pipeline_scope(None):
            assert rl.current_pipeline() == ""

    def test_nested_scopes_restore_outer(self):
        with rl.pipeline_scope("scan"):
            with rl.pipeline_scope("ipo"):
                assert rl.current_pipeline() == "ipo"
            assert rl.current_pipeline() == "scan"

    def test_restored_even_on_exception(self):
        with pytest.raises(ValueError):
            with rl.pipeline_scope("scan"):
                raise ValueError("x")
        assert rl.current_pipeline() == ""

    def test_context_var_is_used_when_thread_local_is_empty(self):
        token = rl._pipeline_ctx.set("Surprise ")
        try:
            assert rl.current_pipeline() == "surprise"
        finally:
            rl._pipeline_ctx.reset(token)

    def test_thread_local_wins_over_context_var(self, monkeypatch):
        monkeypatch.setattr(rl._pipeline_tls, "name", "scan", raising=False)
        token = rl._pipeline_ctx.set("ipo")
        try:
            assert rl.current_pipeline() == "scan"
        finally:
            rl._pipeline_ctx.reset(token)

    def test_context_var_failure_yields_empty(self, monkeypatch):
        class Boom:
            def get(self, default=""):
                raise RuntimeError("ctx broken")

        monkeypatch.setattr(rl, "_pipeline_ctx", Boom())
        assert rl.current_pipeline() == ""

    def test_context_reset_failure_is_suppressed(self, monkeypatch):
        class Ctx:
            def set(self, v):
                return "tok"

            def get(self, default=""):
                return ""

            def reset(self, tok):
                raise ValueError("wrong context")

        monkeypatch.setattr(rl, "_pipeline_ctx", Ctx())
        with rl.pipeline_scope("scan"):
            assert rl.current_pipeline() == "scan"
        assert rl.current_pipeline() == ""

    @pytest.mark.parametrize("arg,expected", [
        ("scan", True), (" SCAN ", True), ("hotpicks", True),
        ("lookup", False), ("", False), (None, False),
    ])
    def test_is_background_explicit(self, arg, expected):
        assert rl._is_background(arg) is expected

    def test_is_background_uses_current_scope(self):
        assert rl._is_background(None) is False
        with rl.pipeline_scope("ipo"):
            assert rl._is_background(None) is True
            assert rl._is_background("lookup") is False


# ─────────────────────────────────── _cfg ───────────────────────────────────

class TestCfg:
    @pytest.mark.parametrize("provider,expected", [
        ("yfinance", (2.0, 6)), ("indianapi", (1.0, 3)), ("nse", (1.0, 3)),
        ("market_data", (8.0, 20)), ("analysis", (3.0, 8)),
    ])
    def test_known_defaults(self, provider, expected):
        assert rl._cfg(provider) == expected

    def test_unknown_provider_default(self):
        assert rl._cfg("brandnew") == (2.0, 5)

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("RL_YFINANCE_RPS", "4.5")
        monkeypatch.setenv("RL_YFINANCE_BURST", "10")
        assert rl._cfg("yfinance") == (4.5, 10)

    def test_env_applies_to_unknown_provider(self, monkeypatch):
        monkeypatch.setenv("RL_BRANDNEW_RPS", "9")
        assert rl._cfg("brandnew") == (9.0, 5)

    def test_bad_burst_keeps_earlier_rps_override(self, monkeypatch):
        monkeypatch.setenv("RL_NSE_RPS", "7")
        monkeypatch.setenv("RL_NSE_BURST", "lots")
        assert rl._cfg("nse") == (7.0, 3)

    def test_bad_rps_keeps_defaults(self, monkeypatch):
        monkeypatch.setenv("RL_NSE_RPS", "fast")
        monkeypatch.setenv("RL_NSE_BURST", "9")
        assert rl._cfg("nse") == (1.0, 3)


# ───────────────────────────────── _Bucket ─────────────────────────────────

class TestBucketBasics:
    def test_starts_full(self):
        b = rl._Bucket(rps=2.0, capacity=6.0)
        assert b.tokens == 6.0 and b.waiters == 0

    def test_snapshot(self, clock):
        b = bucket(clock, rps=2, capacity=6)
        b.tokens = 3.14159
        b.last_wait_sec = 0.123456
        b.throttle_events, b.denied_events, b.waiters = 4, 2, 1
        assert b.snapshot() == {
            "rps": 2, "capacity": 6, "tokens_available": 3.14, "waiters": 1,
            "throttle_events": 4, "denied_events": 2, "last_wait_sec": 0.12,
        }

    def test_warn_throttled_warns_then_debugs_within_30s(self, clock, log):
        b = bucket(clock)
        b._warn_throttled("first %s", 1)
        b._warn_throttled("second %s", 2)
        assert [(lv, m) for lv, m in log.rec] == [("warning", "first 1"), ("debug", "second 2")]
        clock.now += 30
        b._warn_throttled("third")
        assert log.rec[-1] == ("warning", "third")

    def test_warn_message_without_args(self, clock, log):
        bucket(clock)._warn_throttled("plain")
        assert log.rec == [("warning", "plain")]


class TestBucketAcquire:
    def test_immediate_when_tokens_available(self, clock):
        b = bucket(clock, rps=2, capacity=5)
        assert b.acquire(weight=2) == 0.0
        assert b.tokens == 3.0
        assert (b.throttle_events, b.denied_events, b.waiters, b.last_wait_sec) == (0, 0, 0, 0.0)
        assert clock.sleeps == []

    def test_refill_is_capped_at_capacity(self, clock):
        b = bucket(clock, rps=1, capacity=3, tokens=0.0)
        clock.now += 100
        assert b.acquire(weight=1) == 0.0
        assert b.tokens == 2.0

    def test_waits_for_refill_then_succeeds(self, clock):
        b = bucket(clock, rps=1, capacity=2, tokens=0.0)
        assert b.acquire(weight=1, max_wait=5) == 1.0
        assert clock.sleeps == [1.0]
        assert b.throttle_events == 1 and b.last_wait_sec == 1.0
        assert b.tokens == 0.0 and b.waiters == 0

    def test_default_budget_is_module_constant(self, clock, monkeypatch):
        monkeypatch.setattr(rl, "MAX_WAIT_DEFAULT", 1.0)
        b = bucket(clock, rps=0.0, capacity=1, tokens=0.0)
        assert b.acquire(weight=1, fail_fast=True) == -1.0
        assert clock.now == 1001.0

    def test_sleep_is_capped_at_two_seconds(self, clock):
        b = bucket(clock, rps=0.1, capacity=1, tokens=0.0)
        assert b.acquire(weight=1, max_wait=5, fail_fast=True) == -1.0
        assert clock.sleeps == [2.0, 2.0, 2.0]

    def test_sleep_is_clamped_to_budget(self, clock):
        b = bucket(clock, rps=1, capacity=1, tokens=0.0)
        assert b.acquire(weight=1, max_wait=0.1, fail_fast=True) == -1.0  # budget floors at 0.5
        assert clock.sleeps == [0.5]

    def test_sleep_has_a_50ms_floor(self, clock):
        b = bucket(clock, rps=100, capacity=1, tokens=0.99)
        b.acquire(weight=1, max_wait=5)
        assert clock.sleeps == [0.05]

    def test_zero_rate_polls_every_half_second(self, clock, log):
        b = bucket(clock, rps=0.0, capacity=1, tokens=0.0)
        assert b.acquire(weight=1, max_wait=1.0, fail_fast=True) == -1.0
        assert clock.sleeps == [0.5, 0.5]
        assert b.denied_events == 1 and b.waiters == 0
        assert "skipping (weight=1, queue=1)" in log.text("warning")

    def test_proceeds_anyway_when_not_fail_fast(self, clock, log):
        b = bucket(clock, rps=0.0, capacity=1, tokens=0.3)
        waited = b.acquire(weight=2, max_wait=1.0)
        assert waited == 1.0
        assert b.tokens == 0.0
        assert b.denied_events == 1 and b.throttle_events == 0
        assert "proceeding (weight=2, queue=1, reserve=0.0)" in log.text("warning")

    def test_repeat_denials_are_logged_at_debug(self, clock, log):
        b = bucket(clock, rps=0.0, capacity=1, tokens=0.0)
        b.acquire(weight=1, max_wait=1.0, fail_fast=True)
        b.acquire(weight=1, max_wait=1.0, fail_fast=True)
        assert [lv for lv, _ in log.rec] == ["warning", "debug"]
        assert b.denied_events == 2

    def test_budget_shrinks_with_queue_depth(self, clock, log):
        b = bucket(clock, rps=0.0, capacity=1, tokens=0.0)
        b.waiters = 3  # three callers already queued -> depth 4 -> 8s / 4 = 2s
        assert b.acquire(weight=1, max_wait=8, fail_fast=True) == -1.0
        assert clock.sleeps == [0.5] * 4
        assert "queue=4" in log.text("warning")
        assert b.waiters == 3

    def test_budget_never_below_floor(self, clock):
        b = bucket(clock, rps=0.0, capacity=1, tokens=0.0)
        b.waiters = 9
        b.acquire(weight=1, max_wait=1.0, fail_fast=True)
        assert clock.sleeps == [0.5]

    def test_reserve_keeps_tokens_back(self, clock):
        b = bucket(clock, rps=1, capacity=8, tokens=8.0)
        assert b.acquire(weight=6, reserve=2.0) == 0.0
        assert b.tokens == 2.0
        assert b.acquire(weight=1, reserve=2.0, max_wait=5) == 1.0  # had to wait for refill
        assert b.tokens == 2.0
        assert clock.sleeps == [1.0]

    def test_unreachable_weight_under_reserve_is_denied(self, clock):
        b = bucket(clock, rps=1, capacity=8, tokens=8.0)
        assert b.acquire(weight=7, reserve=2.0, max_wait=1.0, fail_fast=True) == -1.0

    def test_waiters_restored_when_sleep_raises(self, clock):
        b = bucket(clock, rps=1, capacity=1, tokens=0.0)
        clock.sleep_raises = KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):
            b.acquire(weight=1, max_wait=5)
        assert b.waiters == 0

    def test_waiters_counter_never_negative(self, clock):
        b = bucket(clock, rps=1, capacity=2)
        b.acquire(weight=1)
        b.waiters = 0
        b.acquire(weight=1)
        assert b.waiters == 0


# ───────────────────────── registry / module helpers ─────────────────────────

class TestBucketRegistry:
    def test_get_bucket_creates_once_with_cfg(self, monkeypatch):
        monkeypatch.setenv("RL_YFINANCE_BURST", "9")
        b = rl._get_bucket("yfinance")
        assert b is rl._get_bucket("yfinance")
        assert (b.rps, b.capacity, b.tokens) == (2.0, 9.0, 9.0)
        assert isinstance(b.capacity, float)

    def test_distinct_providers(self):
        assert rl._get_bucket("nse") is not rl._get_bucket("indianapi")

    def test_reserve_for_interactive_is_zero(self):
        b = rl._Bucket(rps=1, capacity=10)
        assert rl._reserve_for(b, None) == 0.0
        assert rl._reserve_for(b, "lookup") == 0.0

    def test_reserve_for_background_is_fraction_of_capacity(self, monkeypatch):
        monkeypatch.setattr(rl, "INTERACTIVE_RESERVE_FRACTION", 0.5)
        b = rl._Bucket(rps=1, capacity=10)
        assert rl._reserve_for(b, "scan") == 5.0
        with rl.pipeline_scope("ipo"):
            assert rl._reserve_for(b, None) == 5.0

    def test_reserve_for_never_negative(self, monkeypatch):
        monkeypatch.setattr(rl, "INTERACTIVE_RESERVE_FRACTION", -1.0)
        assert rl._reserve_for(rl._Bucket(rps=1, capacity=10), "scan") == 0.0

    def test_stats(self):
        assert rl.stats() == {}
        rl._get_bucket("nse")
        rl._get_bucket("yfinance")
        s = rl.stats()
        assert set(s) == {"nse", "yfinance"}
        assert s["nse"]["capacity"] == 3.0 and s["nse"]["tokens_available"] == 3.0


class FakeBucket:
    def __init__(self, result=0.25, raises=None, capacity=10.0):
        self.result, self.raises, self.capacity = result, raises, capacity
        self.calls = []

    def acquire(self, **kw):
        self.calls.append(kw)
        if self.raises:
            raise self.raises
        return self.result


class TestAcquireApi:
    def test_acquire_passes_arguments_through(self, monkeypatch):
        fb = FakeBucket(result=0.25)
        monkeypatch.setattr(rl, "_get_bucket", lambda p: fb)
        assert rl.acquire("nse", weight=3, max_wait=9.0) == 0.25
        assert fb.calls == [{"weight": 3, "max_wait": 9.0, "reserve": 0.0}]

    def test_acquire_reserves_for_background_pipeline(self, monkeypatch):
        monkeypatch.setattr(rl, "INTERACTIVE_RESERVE_FRACTION", 0.5)
        fb = FakeBucket()
        monkeypatch.setattr(rl, "_get_bucket", lambda p: fb)
        rl.acquire("nse", pipeline="scan")
        assert fb.calls[0]["reserve"] == 5.0

    def test_acquire_fails_open(self, monkeypatch, log):
        monkeypatch.setattr(rl, "_get_bucket", lambda p: FakeBucket(raises=RuntimeError("bucket broke")))
        assert rl.acquire("nse") == 0.0
        assert "acquire(nse) failed open: bucket broke" in log.text("debug")

    def test_acquire_real_bucket_end_to_end(self, clock):
        rl._get_bucket("nse").updated = clock.now  # bucket clock default is the real time.time
        assert rl.acquire("nse", weight=1) == 0.0
        assert rl.stats()["nse"]["tokens_available"] == 2.0

    def test_try_acquire_true_on_success(self, monkeypatch):
        fb = FakeBucket(result=0.0)
        monkeypatch.setattr(rl, "_get_bucket", lambda p: fb)
        assert rl.try_acquire("nse", weight=2, max_wait=1.0) is True
        assert fb.calls == [{"weight": 2, "max_wait": 1.0, "reserve": 0.0, "fail_fast": True}]

    def test_try_acquire_false_when_denied(self, monkeypatch):
        monkeypatch.setattr(rl, "_get_bucket", lambda p: FakeBucket(result=-1.0))
        assert rl.try_acquire("nse") is False

    def test_try_acquire_fails_open(self, monkeypatch, log):
        monkeypatch.setattr(rl, "_get_bucket", lambda p: FakeBucket(raises=RuntimeError("x")))
        assert rl.try_acquire("nse") is True
        assert "try_acquire(nse) failed open" in log.text("debug")

    def test_try_acquire_real_bucket_denies_when_drained(self, clock):
        b = rl._get_bucket("nse")
        b.tokens, b.updated, b.rps = 0.0, clock.now, 0.0
        assert rl.try_acquire("nse", max_wait=1.0) is False


class TestSuggestedTimeout:
    @pytest.mark.parametrize("waiters,expected", [(0, 10.0), (3, 15.0), (6, 20.0), (60, 20.0)])
    def test_scales_with_contention(self, waiters, expected):
        rl._get_bucket("yfinance").waiters = waiters
        assert rl.suggested_timeout(10.0, "yfinance") == expected

    def test_floor(self):
        assert rl.suggested_timeout(0.2, "yfinance") == 1.0
        assert rl.suggested_timeout(0.2, "yfinance", floor=0.1) == 0.2

    def test_returns_base_on_error(self, monkeypatch):
        def boom(p):
            raise RuntimeError("no bucket")

        monkeypatch.setattr(rl, "_get_bucket", boom)
        assert rl.suggested_timeout(7.0, "yfinance") == 7.0


# ─────────────────────────── symbol_aliases bridge ───────────────────────────

class TestAliasesHelpers:
    def test_aliases_returns_module(self, sa):
        assert rl._aliases() is sa

    def test_aliases_none_when_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "symbol_aliases", None)
        assert rl._aliases() is None

    @pytest.mark.parametrize("raw,expected", [
        ("reliance.ns", "RELIANCE"), ("TCS.BO", "TCS"), ("  infy ", "INFY"),
        ("", ""), (None, ""), (123, "123"),
    ])
    def test_base_symbol(self, raw, expected):
        assert rl._base_symbol(raw) == expected


class TestIsSkippable:
    def test_false_without_aliases(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "symbol_aliases", None)
        assert rl.is_skippable("TCS.NS") is False

    def test_false_for_blank_symbol(self, sa):
        assert rl.is_skippable("") is False
        assert rl.is_skippable(None) is False

    def test_true_when_learned_delisted(self, sa):
        sa.delisted.add("DEAD")
        assert rl.is_skippable("dead.ns") is True

    def test_false_when_alive(self, sa):
        assert rl.is_skippable("TCS.NS") is False

    def test_delisted_lookup_error_is_false(self, sa):
        sa.raise_on.add("is_learned_delisted")
        sa.high_price.add("MRF")
        assert rl.is_skippable("MRF") is False

    def test_high_price_ignored_by_default(self, sa):
        sa.high_price.add("MRF")
        assert rl.is_skippable("MRF") is False

    def test_high_price_when_opted_in(self, sa, monkeypatch):
        monkeypatch.setattr(rl, "SKIP_HIGH_PRICE", True)
        sa.high_price.add("MRF")
        assert rl.is_skippable("MRF.NS") is True
        assert rl.is_skippable("TCS") is False

    def test_high_price_lookup_error_is_false(self, sa, monkeypatch):
        monkeypatch.setattr(rl, "SKIP_HIGH_PRICE", True)
        sa.raise_on.add("is_known_high_price")
        assert rl.is_skippable("MRF") is False


class TestDiscoverRenameAsync:
    def test_disabled_flag(self, sa, inline_threads, monkeypatch):
        monkeypatch.setattr(rl, "RENAME_DISCOVERY", False)
        rl._discover_rename_async("ZOMATO")
        assert FakeThread.made == [] and rl._DISCOVERY_TRIED == set()

    def test_blank_symbol(self, sa, inline_threads):
        rl._discover_rename_async("")
        assert FakeThread.made == []

    def test_only_once_per_symbol(self, sa, inline_threads):
        rl._discover_rename_async("ZOMATO")
        rl._discover_rename_async("ZOMATO")
        assert len(FakeThread.made) == 1
        assert "ZOMATO" in rl._DISCOVERY_TRIED

    def test_thread_is_named_daemon(self, sa, inline_threads):
        rl._discover_rename_async("ZOMATO")
        t = FakeThread.made[0]
        assert t.name == "rename-discovery-ZOMATO" and t.daemon is True

    def test_rename_found_clears_streak_and_warns(self, sa, inline_threads, log):
        sa.discover_result = "ETERNAL"
        rl._discover_rename_async("ZOMATO")
        assert sa.discover_calls == [("ZOMATO", 8.0)]
        assert sa.cleared == ["ZOMATO"]
        assert "symbol rename discovered: ZOMATO -> ETERNAL" in log.text("warning")

    def test_no_rename_found(self, sa, inline_threads, log):
        rl._discover_rename_async("ZOMATO")
        assert sa.discover_calls and sa.cleared == []
        assert log.text("warning") == ""

    def test_clear_failure_is_swallowed(self, sa, inline_threads, log):
        sa.discover_result = "ETERNAL"
        sa.raise_on.add("clear_resolution_failures")
        rl._discover_rename_async("ZOMATO")
        assert "symbol rename discovered" in log.text("warning")

    def test_discovery_error_is_logged(self, sa, inline_threads, log):
        sa.raise_on.add("try_discover_rename")
        rl._discover_rename_async("ZOMATO")
        assert "rename discovery for ZOMATO failed: try_discover_rename exploded" in log.text("debug")

    def test_slot_released_after_work(self, sa, inline_threads):
        rl._discover_rename_async("ZOMATO")
        assert rl._DISCOVERY_SLOT.acquire(blocking=False) is True

    def test_busy_slot_drops_attempt_and_allows_retry(self, sa, inline_threads):
        assert rl._DISCOVERY_SLOT.acquire(blocking=False) is True  # someone else is discovering
        rl._discover_rename_async("ZOMATO")
        assert sa.discover_calls == []
        assert "ZOMATO" not in rl._DISCOVERY_TRIED
        rl._DISCOVERY_SLOT.release()
        rl._discover_rename_async("ZOMATO")
        assert sa.discover_calls == [("ZOMATO", 8.0)]

    def test_no_aliases_module_is_a_noop(self, monkeypatch, inline_threads):
        monkeypatch.setitem(sys.modules, "symbol_aliases", None)
        rl._discover_rename_async("ZOMATO")
        assert FakeThread.made and rl._DISCOVERY_SLOT.acquire(blocking=False) is True

    def test_release_failure_is_swallowed(self, sa, inline_threads, monkeypatch):
        class Slot:
            def acquire(self, blocking=True):
                return True

            def release(self):
                raise ValueError("released too many times")

        monkeypatch.setattr(rl, "_DISCOVERY_SLOT", Slot())
        rl._discover_rename_async("ZOMATO")  # must not raise
        assert sa.discover_calls

    def test_thread_start_failure_is_swallowed(self, sa, inline_threads):
        FakeThread.start_raises = RuntimeError("can't start new thread")
        rl._discover_rename_async("ZOMATO")
        assert sa.discover_calls == []

    def test_thread_construction_failure_is_swallowed(self, sa, inline_threads):
        FakeThread.ctor_raises = RuntimeError("no threads")
        rl._discover_rename_async("ZOMATO")


class TestNoteSymbol:
    def test_failure_records_and_normalises(self, sa, inline_threads):
        rl.note_symbol_failure("reliance.ns")
        assert sa.failures == ["RELIANCE"]

    def test_failure_without_aliases(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "symbol_aliases", None)
        rl.note_symbol_failure("TCS")  # no error

    def test_failure_blank_symbol(self, sa):
        rl.note_symbol_failure("")
        assert sa.failures == []

    def test_failure_record_error_is_swallowed(self, sa, monkeypatch):
        sa.raise_on.add("record_resolution_failure")
        seen = []
        monkeypatch.setattr(rl, "_discover_rename_async", seen.append)
        rl.note_symbol_failure("TCS")
        assert seen == []

    def test_discovery_only_at_exact_streak(self, sa, monkeypatch):
        seen = []
        monkeypatch.setattr(rl, "_discover_rename_async", seen.append)
        for streak, expect in [(1, []), (2, ["ZOMATO"]), (3, ["ZOMATO"])]:
            sa.streak = streak
            rl.note_symbol_failure("ZOMATO.NS")
            assert seen == expect

    def test_discovery_error_is_swallowed(self, sa, monkeypatch):
        sa.streak = 2

        def boom(base):
            raise RuntimeError("x")

        monkeypatch.setattr(rl, "_discover_rename_async", boom)
        rl.note_symbol_failure("ZOMATO")

    def test_success_clears_streak(self, sa):
        rl.note_symbol_success("tcs.ns")
        assert sa.cleared == ["TCS"]

    def test_success_without_aliases(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "symbol_aliases", None)
        rl.note_symbol_success("TCS")

    def test_success_blank_symbol(self, sa):
        rl.note_symbol_success(None)
        assert sa.cleared == []

    def test_success_error_is_swallowed(self, sa):
        sa.raise_on.add("clear_resolution_failures")
        rl.note_symbol_success("TCS")


# ─────────────────────────── _looks_empty / _empty_frame ───────────────────────────

class TestLooksEmpty:
    def test_none(self):
        assert rl._looks_empty(None) is True

    def test_frame_like(self):
        assert rl._looks_empty(types.SimpleNamespace(empty=True)) is True
        assert rl._looks_empty(types.SimpleNamespace(empty=False)) is False

    def test_dicts(self):
        assert rl._looks_empty({}) is True
        assert rl._looks_empty({"a": 1}) is False

    def test_other_objects_are_not_empty(self):
        assert rl._looks_empty([]) is False
        assert rl._looks_empty("x") is False
        assert rl._looks_empty(0) is False

    def test_broken_empty_attribute_is_not_empty(self):
        class Bad:
            @property
            def empty(self):
                raise RuntimeError("boom")

        assert rl._looks_empty(Bad()) is False

    def test_unbool_able_empty_attribute_is_not_empty(self):
        class NoBool:
            def __bool__(self):
                raise RuntimeError("ambiguous")

        assert rl._looks_empty(types.SimpleNamespace(empty=NoBool())) is False


class TestEmptyFrame:
    def test_returns_dataframe(self, monkeypatch):
        fake = types.ModuleType("pandas")
        fake.DataFrame = lambda: "DF"
        monkeypatch.setitem(sys.modules, "pandas", fake)
        assert rl._empty_frame() == "DF"

    def test_none_when_pandas_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "pandas", None)
        assert rl._empty_frame() is None


# ─────────────────────────── hard timeout wrapper ───────────────────────────

class FakeFuture:
    def __init__(self, value=None, raises=None):
        self.value, self.raises, self.timeout_seen = value, raises, None

    def result(self, timeout=None):
        self.timeout_seen = timeout
        if self.raises:
            raise self.raises
        return self.value


class FakePool:
    def __init__(self, future):
        self.future, self.submitted = future, []

    def submit(self, fn, *a, **k):
        self.submitted.append((fn, a, k))
        return self.future


class TestHardTimeout:
    def test_returns_result_and_passes_args(self, monkeypatch):
        fut = FakeFuture(value="OK")
        pool = FakePool(fut)
        monkeypatch.setattr(rl, "_yf_hardcap_pool", pool)
        monkeypatch.setattr(rl, "YFINANCE_HARD_TIMEOUT_SEC", 7.0)
        fn = lambda *a, **k: None
        assert rl._yf_call_with_hard_timeout(fn, 1, 2, x=3) == "OK"
        assert pool.submitted == [(fn, (1, 2), {"x": 3})]
        assert fut.timeout_seen == 7.0

    def test_timeout_becomes_timeout_error(self, monkeypatch, log):
        monkeypatch.setattr(rl, "_yf_hardcap_pool", FakePool(FakeFuture(raises=rl._cf.TimeoutError())))
        monkeypatch.setattr(rl, "YFINANCE_HARD_TIMEOUT_SEC", 18.0)
        with pytest.raises(TimeoutError) as ei:
            rl._yf_call_with_hard_timeout(lambda: None)
        assert "exceeded 18s hard timeout" in str(ei.value)
        assert "exceeded hard timeout of 18s" in log.text("warning")

    def test_other_errors_propagate(self, monkeypatch):
        monkeypatch.setattr(rl, "_yf_hardcap_pool", FakePool(FakeFuture(raises=ValueError("upstream 500"))))
        with pytest.raises(ValueError):
            rl._yf_call_with_hard_timeout(lambda: None)

    def test_real_pool_runs_the_call(self):
        assert rl._yf_call_with_hard_timeout(lambda a, b=0: a + b, 2, b=5) == 7

    def test_real_pool_propagates_exceptions(self):
        def boom():
            raise KeyError("k")

        with pytest.raises(KeyError):
            rl._yf_call_with_hard_timeout(boom)


# ───────────────────────────── patch_yfinance ─────────────────────────────

def make_yf(monkeypatch, info_property=True, with_history=True):
    state = types.SimpleNamespace(
        download=types.SimpleNamespace(empty=False), history=types.SimpleNamespace(empty=False),
        info={"symbol": "X"}, raises=None,
        download_calls=[], history_calls=[], info_calls=[],
    )

    class Ticker:
        def __init__(self, ticker):
            self.ticker = ticker

        if with_history:
            def history(self, *a, **k):
                state.history_calls.append((getattr(self, "ticker", ""), a, k))
                if state.raises:
                    raise state.raises
                return state.history

        if info_property:
            @property
            def info(self):
                state.info_calls.append(self.ticker)
                if state.raises:
                    raise state.raises
                return state.info
        else:
            info = {"static": True}

    def download(*a, **k):
        state.download_calls.append((a, k))
        if state.raises:
            raise state.raises
        return state.download

    mod = types.ModuleType("yfinance")
    mod.Ticker, mod.download = Ticker, download
    monkeypatch.setitem(sys.modules, "yfinance", mod)
    state.mod, state.orig_download = mod, download
    return state


@pytest.fixture
def gate(monkeypatch):
    """Record the collaborators of the patched yfinance entry points."""
    g = types.SimpleNamespace(acquired=[], failed=[], succeeded=[], dead=set(), wait=0.0)
    monkeypatch.setattr(rl, "acquire", lambda provider, weight=1.0, **k: g.acquired.append((provider, weight)) or g.wait)
    monkeypatch.setattr(rl, "note_symbol_failure", g.failed.append)
    monkeypatch.setattr(rl, "note_symbol_success", g.succeeded.append)
    monkeypatch.setattr(rl, "is_skippable", lambda s: s in g.dead)
    monkeypatch.setattr(rl, "_empty_frame", lambda: "EMPTY")
    monkeypatch.setattr(rl, "_yf_call_with_hard_timeout", lambda fn, *a, **k: fn(*a, **k))
    return g


class TestPatchYfinance:
    def test_missing_yfinance_returns_false(self, monkeypatch, log):
        monkeypatch.setitem(sys.modules, "yfinance", None)
        assert rl.patch_yfinance() is False
        assert rl._yf_patched is False
        assert "yfinance not importable" in log.text("warning")

    def test_patches_and_is_idempotent(self, monkeypatch, gate, log):
        st = make_yf(monkeypatch)
        assert rl.patch_yfinance() is True and rl._yf_patched is True
        patched = st.mod.download
        assert patched is not st.orig_download
        assert rl.patch_yfinance() is True
        assert st.mod.download is patched
        assert "patch active" in log.text("info")

    def test_ticker_patch_failure_is_logged_but_download_stays_patched(self, monkeypatch, gate, log):
        st = make_yf(monkeypatch, with_history=False)
        assert rl.patch_yfinance() is True
        assert "could not patch Ticker.history/.info" in log.text("warning")
        assert st.mod.download is not st.orig_download

    def test_non_property_info_is_left_alone(self, monkeypatch, gate):
        st = make_yf(monkeypatch, info_property=False)
        rl.patch_yfinance()
        assert st.mod.Ticker.info == {"static": True}
        assert st.mod.Ticker.history.__name__ == "_patched_history"


class TestPatchedDownload:
    def _go(self, monkeypatch):
        st = make_yf(monkeypatch)
        rl.patch_yfinance()
        return st

    def test_all_dead_batch_skips_network_and_limiter(self, monkeypatch, gate):
        st = self._go(monkeypatch)
        gate.dead = {"A.NS", "B.NS"}
        assert st.mod.download("A.NS B.NS") == "EMPTY"
        assert st.download_calls == [] and gate.acquired == []

    def test_partially_dead_batch_still_downloads(self, monkeypatch, gate):
        st = self._go(monkeypatch)
        gate.dead = {"A.NS"}
        assert st.mod.download("A.NS B.NS") is st.download
        assert len(st.download_calls) == 1

    def test_tickers_kwarg_is_read(self, monkeypatch, gate):
        st = self._go(monkeypatch)
        gate.dead = {"A.NS"}
        assert st.mod.download(tickers="A.NS", period="1d") == "EMPTY"

    @pytest.mark.parametrize("tickers,weight", [("A.NS", 1), ("A.NS B.NS", 2), (" ".join(f"S{i}.NS" for i in range(50)), 2)])
    def test_weight_is_a_small_constant(self, monkeypatch, gate, tickers, weight):
        st = self._go(monkeypatch)
        st.mod.download(tickers)
        assert gate.acquired == [("yfinance", weight)]

    def test_arguments_reach_the_real_download(self, monkeypatch, gate):
        st = self._go(monkeypatch)
        st.mod.download("A.NS B.NS", period="1y", progress=False)
        assert st.download_calls == [(("A.NS B.NS",), {"period": "1y", "progress": False})]

    def test_long_wait_is_logged(self, monkeypatch, gate, log):
        st = self._go(monkeypatch)
        gate.wait = 3.2
        st.mod.download("A.NS B.NS")
        assert "held download() for 3.2s (weight=2, symbols=2)" in log.text("info")

    def test_short_wait_is_not_logged(self, monkeypatch, gate, log):
        st = self._go(monkeypatch)
        gate.wait = 0.5
        st.mod.download("A.NS")
        assert "held download()" not in log.text("info")

    def test_single_ticker_success_resets_streak(self, monkeypatch, gate):
        st = self._go(monkeypatch)
        st.mod.download("A.NS")
        assert gate.succeeded == ["A.NS"] and gate.failed == []

    def test_single_ticker_empty_counts_as_failure(self, monkeypatch, gate):
        st = self._go(monkeypatch)
        st.download = types.SimpleNamespace(empty=True)
        st.mod.download("A.NS")
        assert gate.failed == ["A.NS"] and gate.succeeded == []

    def test_batch_result_is_not_attributed_per_symbol(self, monkeypatch, gate):
        st = self._go(monkeypatch)
        st.download = types.SimpleNamespace(empty=True)
        st.mod.download("A.NS B.NS")
        assert gate.failed == [] and gate.succeeded == []

    def test_empty_tickers_does_not_raise(self, monkeypatch, gate):
        # regression: used to IndexError on symbols[0] after the real call returned
        st = self._go(monkeypatch)
        assert st.mod.download("") is st.download
        assert st.mod.download() is st.download
        assert gate.failed == [] and gate.succeeded == []

    def test_download_errors_propagate(self, monkeypatch, gate):
        st = self._go(monkeypatch)
        st.raises = RuntimeError("429")
        with pytest.raises(RuntimeError):
            st.mod.download("A.NS")


class TestPatchedTicker:
    def _go(self, monkeypatch):
        st = make_yf(monkeypatch)
        rl.patch_yfinance()
        return st

    def test_history_success(self, monkeypatch, gate):
        st = self._go(monkeypatch)
        assert st.mod.Ticker("A.NS").history(period="5d") is st.history
        assert gate.acquired == [("yfinance", 1)]
        assert gate.succeeded == ["A.NS"]
        assert st.history_calls == [("A.NS", (), {"period": "5d"})]

    def test_history_empty_is_failure(self, monkeypatch, gate):
        st = self._go(monkeypatch)
        st.history = types.SimpleNamespace(empty=True)
        st.mod.Ticker("A.NS").history()
        assert gate.failed == ["A.NS"]

    def test_history_skips_dead_symbol(self, monkeypatch, gate):
        st = self._go(monkeypatch)
        gate.dead = {"DEAD.NS"}
        assert st.mod.Ticker("DEAD.NS").history() == "EMPTY"
        assert st.history_calls == [] and gate.acquired == []

    def test_history_exception_counts_failure_and_reraises(self, monkeypatch, gate):
        st = self._go(monkeypatch)
        st.raises = RuntimeError("Invalid Crumb")
        with pytest.raises(RuntimeError):
            st.mod.Ticker("A.NS").history()
        assert gate.failed == ["A.NS"]

    def test_info_success_uses_weight_two(self, monkeypatch, gate):
        st = self._go(monkeypatch)
        assert st.mod.Ticker("A.NS").info == {"symbol": "X"}
        assert gate.acquired == [("yfinance", 2)]
        assert gate.succeeded == ["A.NS"]

    def test_info_empty_dict_is_failure(self, monkeypatch, gate):
        st = self._go(monkeypatch)
        st.info = {}
        assert st.mod.Ticker("A.NS").info == {}
        assert gate.failed == ["A.NS"]

    def test_info_skips_dead_symbol(self, monkeypatch, gate):
        st = self._go(monkeypatch)
        gate.dead = {"DEAD.NS"}
        assert st.mod.Ticker("DEAD.NS").info == {}
        assert st.info_calls == [] and gate.acquired == []

    def test_info_exception_counts_failure_and_reraises(self, monkeypatch, gate):
        st = self._go(monkeypatch)
        st.raises = RuntimeError("quoteSummary 401")
        with pytest.raises(RuntimeError):
            st.mod.Ticker("A.NS").info
        assert gate.failed == ["A.NS"]

    def test_ticker_without_symbol_attribute(self, monkeypatch, gate):
        st = self._go(monkeypatch)
        t = st.mod.Ticker.__new__(st.mod.Ticker)  # no .ticker set
        t.history()
        assert gate.succeeded == [""]


class TestPatchedEndToEnd:
    def test_real_limiter_and_real_hard_timeout_pool(self, monkeypatch, sa):
        st = make_yf(monkeypatch)
        rl.patch_yfinance()
        assert st.mod.download("A.NS") is st.download
        assert rl.stats()["yfinance"]["tokens_available"] == 5.0
        assert sa.cleared == ["A"]

    def test_dead_symbol_is_skipped_before_the_limiter(self, monkeypatch, sa):
        st = make_yf(monkeypatch)
        sa.delisted.add("GONE")
        rl.patch_yfinance()
        st.download_calls.clear()
        assert st.mod.Ticker("GONE.NS").info == {}
        assert st.info_calls == [] and "yfinance" not in rl.stats()
