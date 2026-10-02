"""
tests/test_rate_limiter.py — coverage for fundamental/rate_limiter.py

No network, no real sleeping (except one small threaded test). Every test loads a
FRESH copy of the module under a private name, so the module-level bucket registry,
the `_yf_patched` flag and the env-derived constants never leak between tests.

  * FakeClock  — replaces the module's `time` so token-bucket refill / waiting is
                 deterministic: `sleep()` just advances the clock and records the
                 requested duration.
  * fake yfinance — a tiny module (download / Ticker.history / Ticker.info) dropped
                 into sys.modules so `patch_yfinance()` can be exercised end to end.
  * One optional test runs the patch against the REAL pinned yfinance (skipped when
    it isn't installed) and restores it afterwards, to catch a yfinance upgrade that
    changes the shape `patch_yfinance()` relies on (Ticker.info being a property).

Run from services/analysis-intelligence-service:
    python3 -m pytest tests/test_rate_limiter.py -v
"""
from __future__ import annotations

import importlib.util
import itertools
import logging
import os
import sys
import threading
import time as _real_time
import types
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(os.path.dirname(_HERE), "fundamental", "rate_limiter.py")

_ENV_KEYS = (
    "YFINANCE_HARD_TIMEOUT_SEC", "YFINANCE_POOL_WORKERS",
    "RL_YFINANCE_RPS", "RL_YFINANCE_BURST",
    "RL_INDIANAPI_RPS", "RL_INDIANAPI_BURST",
    "RL_NSE_RPS", "RL_NSE_BURST",
    "RL_MARKET_DATA_RPS", "RL_MARKET_DATA_BURST",
    "RL_ANALYSIS_RPS", "RL_ANALYSIS_BURST",
    "RL_FOO_RPS", "RL_FOO_BURST",
)

_counter = itertools.count()
_loaded = []


# ── helpers ───────────────────────────────────────────────────────────────────

class FakeClock:
    """Deterministic stand-in for the `time` module (time() + sleep())."""

    def __init__(self, start=1000.0, tick=0.0):
        self.now = start
        self.tick = tick            # each time() call advances the clock by this much
        self.sleeps = []
        self.on_sleep = None

    def time(self):
        t = self.now
        self.now += self.tick
        return t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


def _load(fake_clock=None):
    """Execute a fresh copy of rate_limiter.py and return the module."""
    name = f"_rate_limiter_under_test_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(name, _PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod            # dataclasses looks the module up by name
    _loaded.append(mod)
    spec.loader.exec_module(mod)
    if fake_clock is not None:
        mod.time = SimpleNamespace(time=fake_clock.time, sleep=fake_clock.sleep)
        mod._clock = fake_clock
    return mod


def _mk(rl, rps, capacity):
    """A bucket anchored to the fake clock (the dataclass default_factory is bound
    to the REAL time.time at class-creation, so pass `updated` explicitly)."""
    return rl._Bucket(rps=rps, capacity=float(capacity), updated=rl._clock.now)


def _prime(rl, provider):
    """Create the provider's bucket via _get_bucket and anchor it to the fake clock."""
    b = rl._get_bucket(provider)
    b.updated = rl._clock.now
    return b


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    yield
    while _loaded:
        mod = _loaded.pop()
        try:
            mod._yf_hardcap_pool.shutdown(wait=False)
        except Exception:
            pass
        sys.modules.pop(mod.__name__, None)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def rl(clock):
    return _load(clock)


# ── _cfg ──────────────────────────────────────────────────────────────────────

class TestCfg:
    def test_known_provider_defaults(self, rl):
        assert rl._cfg("yfinance") == (2.0, 6)
        assert rl._cfg("indianapi") == (1.0, 3)
        assert rl._cfg("nse") == (1.0, 3)
        assert rl._cfg("market_data") == (8.0, 20)
        assert rl._cfg("analysis") == (3.0, 8)

    def test_unknown_provider_gets_generic_default(self, rl):
        assert rl._cfg("something_new") == (2.0, 5)

    def test_env_overrides_both(self, rl, monkeypatch):
        monkeypatch.setenv("RL_NSE_RPS", "0.5")
        monkeypatch.setenv("RL_NSE_BURST", "7")
        assert rl._cfg("nse") == (0.5, 7)

    def test_env_name_is_uppercased_provider(self, rl, monkeypatch):
        monkeypatch.setenv("RL_MARKET_DATA_RPS", "4")
        monkeypatch.setenv("RL_MARKET_DATA_BURST", "9")
        assert rl._cfg("market_data") == (4.0, 9)

    def test_env_applies_to_unknown_provider(self, rl, monkeypatch):
        monkeypatch.setenv("RL_FOO_RPS", "4")
        assert rl._cfg("foo") == (4.0, 5)

    def test_empty_env_values_are_ignored(self, rl, monkeypatch):
        monkeypatch.setenv("RL_NSE_RPS", "")
        monkeypatch.setenv("RL_NSE_BURST", "")
        assert rl._cfg("nse") == (1.0, 3)

    def test_bad_rps_is_swallowed_and_valid_burst_override_still_applies(self, rl, monkeypatch):
        # Each override is parsed on its own: a bad rps falls back to the default
        # rps, but a VALID burst env var is still honoured.
        monkeypatch.setenv("RL_NSE_RPS", "fast")
        monkeypatch.setenv("RL_NSE_BURST", "9")
        assert rl._cfg("nse") == (1.0, 9)

    def test_both_bad_overrides_fall_back_to_defaults(self, rl, monkeypatch):
        monkeypatch.setenv("RL_NSE_RPS", "fast")
        monkeypatch.setenv("RL_NSE_BURST", "lots")
        assert rl._cfg("nse") == (1.0, 3)

    def test_bad_burst_keeps_valid_rps_override(self, rl, monkeypatch):
        monkeypatch.setenv("RL_NSE_RPS", "0.5")
        monkeypatch.setenv("RL_NSE_BURST", "2.5")     # int("2.5") -> ValueError
        assert rl._cfg("nse") == (0.5, 3)

    def test_zero_rps_string_is_accepted(self, rl, monkeypatch):
        monkeypatch.setenv("RL_NSE_RPS", "0")         # "0" is a truthy string
        assert rl._cfg("nse") == (0.0, 3)


# ── _Bucket ───────────────────────────────────────────────────────────────────

class TestBucketBasics:
    def test_starts_full_with_clean_counters(self, rl):
        b = _mk(rl, 2.0, 5)
        assert b.tokens == 5.0
        assert b.waiters == 0
        assert b.throttle_events == 0
        assert b.last_wait_sec == 0.0
        assert hasattr(b.lock, "acquire")

    def test_snapshot_shape_and_rounding(self, rl):
        b = _mk(rl, 2.0, 5)
        b.tokens = 1.23456
        b.waiters = 2
        b.throttle_events = 3
        b.last_wait_sec = 0.12345
        assert b.snapshot() == {
            "rps": 2.0,
            "capacity": 5.0,
            "tokens_available": 1.23,
            "waiters": 2,
            "throttle_events": 3,
            "last_wait_sec": 0.12,
        }


class TestBucketAcquire:
    def test_immediate_acquire_returns_zero_and_spends_tokens(self, rl, clock):
        b = _mk(rl, 2.0, 5)
        assert b.acquire(weight=2) == 0.0
        assert b.tokens == 3.0
        assert b.throttle_events == 0
        assert b.last_wait_sec == 0.0
        assert clock.sleeps == []

    def test_refill_is_capped_at_capacity(self, rl, clock):
        b = _mk(rl, 2.0, 5)
        b.acquire(weight=5)
        clock.advance(1000)
        b.acquire(weight=1)
        assert b.tokens == 4.0                       # 5 (capped) - 1, not 2000-ish

    def test_partial_refill_is_used(self, rl, clock):
        b = _mk(rl, 2.0, 4)
        b.acquire(weight=4)
        clock.advance(1.0)                           # +2 tokens
        assert b.acquire(weight=2) == 0.0
        assert clock.sleeps == []
        assert b.tokens == pytest.approx(0.0)

    def test_blocks_until_tokens_refill(self, rl, clock):
        b = _mk(rl, 1.0, 1)
        b.acquire(weight=1)
        waited = b.acquire(weight=1)
        assert clock.sleeps == [1.0]                 # deficit 1 / rps 1
        assert waited == pytest.approx(1.0, abs=1e-3)
        assert b.throttle_events == 1
        assert b.last_wait_sec == pytest.approx(1.0, abs=1e-3)

    def test_sleep_is_capped_at_two_seconds(self, rl, clock):
        b = _mk(rl, 0.1, 1)
        b.acquire(weight=1)
        b.acquire(weight=1)                          # deficit/rps = 10s, capped
        assert clock.sleeps[0] == 2.0

    def test_sleep_has_a_50ms_floor(self, rl, clock):
        b = _mk(rl, 1000.0, 1)
        b.acquire(weight=1)
        b.acquire(weight=1)                          # deficit/rps = 1ms, floored
        assert clock.sleeps[0] == 0.05

    def test_short_wait_is_not_counted_as_a_throttle_event(self, rl):
        clock = FakeClock(tick=0.03)                 # every time() call costs 30ms
        rl2 = _load(clock)
        b = _mk(rl2, 2.0, 5)
        waited = b.acquire(weight=1)
        assert waited == pytest.approx(0.03, abs=1e-6)
        assert b.throttle_events == 0
        assert b.last_wait_sec == pytest.approx(0.03, abs=1e-6)

    def test_longer_wait_is_counted_as_a_throttle_event(self, rl):
        clock = FakeClock(tick=0.1)
        rl2 = _load(clock)
        b = _mk(rl2, 2.0, 5)
        waited = b.acquire(weight=1)
        assert waited == pytest.approx(0.1, abs=1e-6)
        assert b.throttle_events == 1

    def test_max_wait_exceeded_proceeds_and_drains_tokens(self, rl, clock, caplog):
        b = _mk(rl, 1.0, 1)
        with caplog.at_level(logging.DEBUG, logger="rate-limiter"):
            waited = b.acquire(weight=5, max_wait=3)     # weight > capacity: never satisfiable
        assert clock.sleeps == [2.0, 2.0]
        assert waited == pytest.approx(4.0, abs=1e-3)
        assert b.tokens == 0.0
        assert b.waiters == 0
        assert any("max_wait exceeded" in r.getMessage() for r in caplog.records)
        # Pinned: the give-up path does not record a throttle event / last_wait_sec.
        assert b.throttle_events == 0
        assert b.last_wait_sec == 0.0

    def test_weight_above_burst_capacity_always_stalls_the_full_max_wait(self, rl, clock):
        # yfinance burst is 6; a weight of 7 can never be satisfied (tokens are capped
        # at capacity), so the default max_wait of 20s is always burned.
        _prime(rl, "yfinance")
        waited = rl.acquire("yfinance", weight=7)
        assert waited >= 20.0
        assert sum(clock.sleeps) >= 20.0

    def test_zero_rps_falls_back_to_half_second_polling_until_max_wait(self, rl, clock):
        b = _mk(rl, 0.0, 1)
        b.acquire(weight=1)
        waited = b.acquire(weight=1, max_wait=1.0)
        assert clock.sleeps == [0.5, 0.5]
        assert waited == pytest.approx(1.0, abs=1e-3)
        assert b.tokens == 0.0

    def test_waiters_counted_while_sleeping_and_reset_after(self, rl, clock):
        b = _mk(rl, 1.0, 1)
        b.acquire(weight=1)
        seen = []
        clock.on_sleep = lambda s: seen.append(b.waiters)
        b.acquire(weight=1)
        assert seen == [1]
        assert b.waiters == 0

    def test_waiters_decremented_even_if_sleep_raises(self, rl, clock):
        b = _mk(rl, 1.0, 1)
        b.acquire(weight=1)

        def boom(_s):
            raise RuntimeError("interrupted")

        clock.on_sleep = boom
        with pytest.raises(RuntimeError):
            b.acquire(weight=1)
        assert b.waiters == 0

    def test_waiters_never_goes_negative(self, rl, clock):
        b = _mk(rl, 1.0, 1)
        b.acquire(weight=1)

        def reset(_s):
            b.waiters = 0                             # someone zeroed it mid-wait

        clock.on_sleep = reset
        b.acquire(weight=1)
        assert b.waiters == 0


# ── module-level acquire / _get_bucket ────────────────────────────────────────

class TestGetBucketAndAcquire:
    def test_bucket_is_created_lazily_once_per_provider(self, rl):
        assert rl._buckets == {}
        a = rl._get_bucket("nse")
        assert rl._get_bucket("nse") is a
        assert rl._get_bucket("indianapi") is not a
        assert set(rl._buckets) == {"nse", "indianapi"}

    def test_bucket_uses_defaults_and_capacity_is_float(self, rl):
        b = rl._get_bucket("yfinance")
        assert (b.rps, b.capacity) == (2.0, 6.0)
        assert isinstance(b.capacity, float)

    def test_env_config_is_read_at_bucket_creation_only(self, rl, monkeypatch):
        monkeypatch.setenv("RL_NSE_BURST", "2")
        b = rl._get_bucket("nse")
        assert b.capacity == 2.0
        monkeypatch.setenv("RL_NSE_BURST", "50")
        assert rl._get_bucket("nse").capacity == 2.0     # already built, not re-read

    def test_acquire_returns_zero_when_not_throttled(self, rl):
        _prime(rl, "yfinance")
        assert rl.acquire("yfinance", weight=1) == 0.0
        assert rl._buckets["yfinance"].tokens == 5.0

    def test_acquire_throttles_once_the_burst_is_spent(self, rl, clock):
        _prime(rl, "nse")                                # burst 3, 1 rps
        for _ in range(3):
            assert rl.acquire("nse") == 0.0
        waited = rl.acquire("nse")
        assert waited == pytest.approx(1.0, abs=1e-3)
        assert rl._buckets["nse"].throttle_events == 1

    def test_weight_and_max_wait_are_forwarded(self, rl, monkeypatch):
        seen = {}

        class Stub:
            def acquire(self, weight, max_wait):
                seen.update(weight=weight, max_wait=max_wait)
                return 0.25

        monkeypatch.setattr(rl, "_get_bucket", lambda provider: Stub())
        assert rl.acquire("x", weight=3, max_wait=7) == 0.25
        assert seen == {"weight": 3, "max_wait": 7}

    def test_fails_open_when_bucket_lookup_raises(self, rl, monkeypatch, caplog):
        def boom(provider):
            raise RuntimeError("registry broken")

        monkeypatch.setattr(rl, "_get_bucket", boom)
        with caplog.at_level(logging.DEBUG, logger="rate-limiter"):
            assert rl.acquire("nse") == 0.0
        assert any("failed open" in r.getMessage() for r in caplog.records)

    def test_fails_open_when_bucket_acquire_raises(self, rl):
        _prime(rl, "nse")
        assert rl.acquire("nse", weight="not-a-number") == 0.0     # TypeError inside
        assert rl._buckets["nse"].waiters == 0                     # finally still ran


# ── suggested_timeout ─────────────────────────────────────────────────────────

class TestSuggestedTimeout:
    def test_no_waiters_returns_base(self, rl):
        assert rl.suggested_timeout(25.0, "yfinance") == 25.0

    @pytest.mark.parametrize("waiters,expected", [
        (0, 10.0), (3, 15.0), (6, 20.0), (60, 20.0),
    ])
    def test_scales_with_waiters_up_to_double(self, rl, waiters, expected):
        rl._get_bucket("yfinance").waiters = waiters
        assert rl.suggested_timeout(10.0, "yfinance") == pytest.approx(expected)

    def test_floor_applies(self, rl):
        assert rl.suggested_timeout(0.2, "yfinance") == 1.0
        assert rl.suggested_timeout(0.2, "yfinance", floor=0.1) == 0.2

    def test_falls_back_to_base_on_error_without_applying_floor(self, rl, monkeypatch):
        def boom(provider):
            raise RuntimeError("nope")

        monkeypatch.setattr(rl, "_get_bucket", boom)
        assert rl.suggested_timeout(0.2, "yfinance") == 0.2


# ── stats ─────────────────────────────────────────────────────────────────────

class TestStats:
    def test_empty_before_any_bucket_exists(self, rl):
        assert rl.stats() == {}

    def test_snapshot_per_provider(self, rl):
        _prime(rl, "yfinance")
        _prime(rl, "nse")
        rl.acquire("yfinance", weight=1)
        s = rl.stats()
        assert set(s) == {"yfinance", "nse"}
        assert s["yfinance"] == {
            "rps": 2.0, "capacity": 6.0, "tokens_available": 5.0,
            "waiters": 0, "throttle_events": 0, "last_wait_sec": 0.0,
        }
        assert s["nse"]["tokens_available"] == 3.0

    def test_suggested_timeout_registers_the_bucket_too(self, rl):
        rl.suggested_timeout(5.0, "analysis")
        assert "analysis" in rl.stats()

    def test_returns_a_detached_copy(self, rl):
        _prime(rl, "nse")
        s = rl.stats()
        s["nse"]["waiters"] = 99
        assert rl.stats()["nse"]["waiters"] == 0


# ── hard wall-clock cap ───────────────────────────────────────────────────────

class TestHardTimeout:
    def test_returns_result_and_forwards_args(self, rl):
        assert rl._yf_call_with_hard_timeout(lambda a, b=0: a + b, 2, b=3) == 5

    def test_inner_exception_propagates_unchanged(self, rl):
        def bad():
            raise ValueError("yahoo said no")

        with pytest.raises(ValueError, match="yahoo said no"):
            rl._yf_call_with_hard_timeout(bad)

    def test_stuck_call_raises_timeout_and_logs(self, rl, monkeypatch, caplog):
        monkeypatch.setattr(rl, "YFINANCE_HARD_TIMEOUT_SEC", 0.05)
        gate = threading.Event()
        try:
            with caplog.at_level(logging.WARNING, logger="rate-limiter"):
                with pytest.raises(TimeoutError, match="hard timeout"):
                    rl._yf_call_with_hard_timeout(gate.wait, 5)
        finally:
            gate.set()
        assert any("exceeded hard timeout" in r.getMessage() for r in caplog.records)

    @pytest.mark.skipif(sys.version_info < (3, 11), reason="_cf.TimeoutError aliases TimeoutError from 3.11")
    def test_an_inner_timeouterror_is_reported_as_a_hard_timeout(self, rl, caplog):
        # On 3.11+ concurrent.futures.TimeoutError IS the builtin, so a TimeoutError
        # raised by yfinance itself is caught by the same except and logged as if the
        # 18s ceiling had been hit. Pinned: harmless, but the warning text is misleading.
        def inner():
            raise TimeoutError("inner read timeout")

        with caplog.at_level(logging.WARNING, logger="rate-limiter"):
            with pytest.raises(TimeoutError, match="hard timeout"):
                rl._yf_call_with_hard_timeout(inner)
        assert any("exceeded hard timeout" in r.getMessage() for r in caplog.records)


class TestImportTimeEnv:
    def test_defaults(self):
        rl = _load()
        assert rl.YFINANCE_HARD_TIMEOUT_SEC == 18.0
        assert rl._yf_hardcap_pool._max_workers == 8
        assert rl._yf_hardcap_pool._thread_name_prefix == "yf-hardcap"
        assert rl._yf_patched is False

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("YFINANCE_HARD_TIMEOUT_SEC", "5")
        monkeypatch.setenv("YFINANCE_POOL_WORKERS", "3")
        rl = _load()
        assert rl.YFINANCE_HARD_TIMEOUT_SEC == 5.0
        assert rl._yf_hardcap_pool._max_workers == 3

    def test_bad_timeout_env_breaks_import(self, monkeypatch):
        # Unlike the RL_* settings (swallowed), these two are parsed at import time
        # with no guard, so a typo stops the service from starting. Pinned.
        monkeypatch.setenv("YFINANCE_HARD_TIMEOUT_SEC", "18s")
        with pytest.raises(ValueError):
            _load()

    def test_bad_worker_count_env_breaks_import(self, monkeypatch):
        monkeypatch.setenv("YFINANCE_POOL_WORKERS", "many")
        with pytest.raises(ValueError):
            _load()

    def test_zero_workers_breaks_import(self, monkeypatch):
        monkeypatch.setenv("YFINANCE_POOL_WORKERS", "0")
        with pytest.raises(ValueError):
            _load()


# ── patch_yfinance (fake yfinance) ────────────────────────────────────────────

def _fake_yf(info_is_property=True):
    yf = types.ModuleType("yfinance")
    yf.calls = []

    def download(*args, **kwargs):
        yf.calls.append(("download", args, kwargs))
        return "DF"

    class Ticker:
        def __init__(self, sym="X.NS"):
            self.sym = sym

        def history(self, *args, **kwargs):
            yf.calls.append(("history", self.sym, args, kwargs))
            return "HIST"

        if info_is_property:
            @property
            def info(self):
                yf.calls.append(("info", self.sym))
                return {"sym": self.sym}
        else:
            info = {"static": True}

    yf.download = download
    yf.Ticker = Ticker
    yf._orig_download = download
    yf._orig_history = Ticker.history
    return yf


@pytest.fixture
def yf(monkeypatch):
    fake = _fake_yf()
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    return fake


@pytest.fixture
def acquires(rl, monkeypatch):
    """Record every acquire() the patched wrappers make (and never block)."""
    rec = []

    def fake_acquire(provider, weight=1.0, max_wait=20.0):
        rec.append((provider, weight))
        return 0.0

    monkeypatch.setattr(rl, "acquire", fake_acquire)
    return rec


class TestPatchYfinance:
    def test_patches_and_reports_success(self, rl, yf, caplog):
        with caplog.at_level(logging.INFO, logger="rate-limiter"):
            assert rl.patch_yfinance() is True
        assert rl._yf_patched is True
        assert yf.download is not yf._orig_download
        assert yf.Ticker.history is not yf._orig_history
        assert isinstance(yf.Ticker.info, property)
        assert any("patch active" in r.getMessage() for r in caplog.records)

    def test_is_idempotent(self, rl, yf, monkeypatch):
        assert rl.patch_yfinance() is True
        first = yf.download
        monkeypatch.setitem(sys.modules, "yfinance", None)   # would ImportError if re-imported
        assert rl.patch_yfinance() is True
        assert yf.download is first                            # not wrapped twice

    def test_missing_yfinance_returns_false_and_can_be_retried(self, rl, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "yfinance", None)     # `import yfinance` -> ImportError
        with caplog.at_level(logging.WARNING, logger="rate-limiter"):
            assert rl.patch_yfinance() is False
        assert rl._yf_patched is False
        assert any("not importable" in r.getMessage() for r in caplog.records)
        monkeypatch.setitem(sys.modules, "yfinance", _fake_yf())
        assert rl.patch_yfinance() is True

    @pytest.mark.parametrize("call_args,call_kwargs,weight", [
        (("A.NS",), {}, 1),
        (("A.NS B.NS",), {}, 2),
        ((["A.NS"],), {}, 1),
        ((["A.NS", "B.NS"],), {}, 2),
        ((), {"tickers": "A.NS B.NS C.NS"}, 2),
        ((), {}, 1),                        # no tickers at all -> still weight 1
        (("A.NS,B.NS",), {}, 1),            # comma-joined (no spaces) counts as one token
    ])
    def test_download_weight_is_one_for_single_two_for_batch(
            self, rl, yf, acquires, call_args, call_kwargs, weight):
        rl.patch_yfinance()
        assert yf.download(*call_args, **call_kwargs) == "DF"
        assert acquires == [("yfinance", weight)]
        assert yf.calls == [("download", call_args, call_kwargs)]

    def test_download_logs_when_held_longer_than_a_second(self, rl, yf, monkeypatch, caplog):
        monkeypatch.setattr(rl, "acquire", lambda *a, **k: 1.5)
        rl.patch_yfinance()
        with caplog.at_level(logging.INFO, logger="rate-limiter"):
            yf.download("A.NS B.NS")
        assert any("held download()" in r.getMessage() for r in caplog.records)

    def test_download_is_quiet_for_short_waits(self, rl, yf, monkeypatch, caplog):
        monkeypatch.setattr(rl, "acquire", lambda *a, **k: 0.5)
        rl.patch_yfinance()
        with caplog.at_level(logging.INFO, logger="rate-limiter"):
            yf.download("A.NS")
        assert not any("held download()" in r.getMessage() for r in caplog.records)

    def test_download_is_wall_clock_capped(self, rl, yf, acquires, monkeypatch):
        gate = threading.Event()
        yf.download = lambda *a, **k: gate.wait(5)             # replaced BEFORE patching
        monkeypatch.setattr(rl, "YFINANCE_HARD_TIMEOUT_SEC", 0.05)
        rl.patch_yfinance()
        try:
            with pytest.raises(TimeoutError):
                yf.download("A.NS")
        finally:
            gate.set()

    def test_history_is_gated_with_weight_one(self, rl, yf, acquires):
        rl.patch_yfinance()
        assert yf.Ticker("RELIANCE.NS").history(period="5d") == "HIST"
        assert acquires == [("yfinance", 1)]
        assert yf.calls == [("history", "RELIANCE.NS", (), {"period": "5d"})]

    def test_history_is_wall_clock_capped(self, rl, yf, acquires, monkeypatch):
        gate = threading.Event()
        yf.Ticker.history = lambda self, *a, **k: gate.wait(5)
        monkeypatch.setattr(rl, "YFINANCE_HARD_TIMEOUT_SEC", 0.05)
        rl.patch_yfinance()
        try:
            with pytest.raises(TimeoutError):
                yf.Ticker("A.NS").history()
        finally:
            gate.set()

    def test_info_is_gated_with_weight_two(self, rl, yf, acquires):
        rl.patch_yfinance()
        assert yf.Ticker("TCS.NS").info == {"sym": "TCS.NS"}
        assert acquires == [("yfinance", 2)]
        assert yf.calls == [("info", "TCS.NS")]

    def test_info_is_wall_clock_capped(self, rl, yf, acquires, monkeypatch):
        gate = threading.Event()
        yf.Ticker.info = property(lambda self: gate.wait(5))
        monkeypatch.setattr(rl, "YFINANCE_HARD_TIMEOUT_SEC", 0.05)
        rl.patch_yfinance()
        try:
            with pytest.raises(TimeoutError):
                yf.Ticker("A.NS").info
        finally:
            gate.set()

    def test_non_property_info_is_left_alone_but_history_still_patched(self, rl, monkeypatch, acquires):
        fake = _fake_yf(info_is_property=False)
        monkeypatch.setitem(sys.modules, "yfinance", fake)
        assert rl.patch_yfinance() is True
        assert fake.Ticker.info == {"static": True}
        assert fake.Ticker.history is not fake._orig_history
        assert fake.Ticker("A.NS").history() == "HIST"
        assert acquires == [("yfinance", 1)]

    def test_ticker_patch_failure_is_logged_but_download_stays_patched(self, rl, monkeypatch, caplog):
        fake = _fake_yf()
        fake.Ticker = object                      # no .history -> AttributeError in the inner try
        monkeypatch.setitem(sys.modules, "yfinance", fake)
        with caplog.at_level(logging.WARNING, logger="rate-limiter"):
            assert rl.patch_yfinance() is True
        assert rl._yf_patched is True
        assert fake.download is not fake._orig_download
        assert any("could not patch Ticker" in r.getMessage() for r in caplog.records)


class TestRealYfinanceShape:
    """Guards the assumption patch_yfinance() makes about the pinned yfinance:
    Ticker.history is a plain method and Ticker.info is a property."""

    def test_patch_applies_to_the_installed_yfinance(self):
        real_yf = pytest.importorskip("yfinance")
        missing = object()
        saved_download = real_yf.download
        saved_history = real_yf.Ticker.__dict__.get("history", missing)
        saved_info = real_yf.Ticker.__dict__.get("info", missing)
        rl = _load()
        try:
            assert rl.patch_yfinance() is True
            assert real_yf.download.__name__ == "_patched_download"
            assert real_yf.Ticker.history.__name__ == "_patched_history"
            assert isinstance(real_yf.Ticker.info, property)
            assert real_yf.Ticker.info.fget.__name__ == "_patched_info"
        finally:
            real_yf.download = saved_download
            for attr, val in (("history", saved_history), ("info", saved_info)):
                if val is missing:
                    if attr in real_yf.Ticker.__dict__:
                        delattr(real_yf.Ticker, attr)
                else:
                    setattr(real_yf.Ticker, attr, val)


# ── real threads, real clock ──────────────────────────────────────────────────

class TestThreaded:
    def test_concurrent_callers_are_actually_spaced_out(self):
        rl = _load()                                    # real time on purpose
        b = rl._Bucket(rps=40.0, capacity=1.0)          # 1 burst token, 25ms/token
        results, errors = [], []

        def worker():
            try:
                results.append(b.acquire(weight=1))
            except Exception as e:                      # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        t0 = _real_time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        elapsed = _real_time.time() - t0

        assert not errors
        assert len(results) == 4
        assert all(w >= 0.0 for w in results)
        assert elapsed >= 3 / 40.0 * 0.9                # 3 of the 4 had to wait for refill
        assert b.waiters == 0
        assert b.tokens >= 0.0
        assert b.throttle_events >= 1
