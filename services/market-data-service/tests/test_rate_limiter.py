"""
tests/test_rate_limiter.py — coverage for rate_limiter.py

No yfinance, no Redis, no upstash. patch_yfinance() is tested with a
stub that exposes the same patching surface. _yf_call_with_hard_timeout
and _breaker_allows_call are tested with a fake circuit breaker.

Run from services/market-data-service:
    python3 -m pytest tests/test_rate_limiter.py -v
"""
from __future__ import annotations
import os, sys, time, threading, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import rate_limiter as rl


@pytest.fixture(autouse=True)
def _clean_buckets():
    rl._buckets.clear()
    rl._cooldowns.clear()
    rl._yf_patched = False
    yield
    rl._buckets.clear()
    rl._cooldowns.clear()
    rl._yf_patched = False


# ══════════════════════════════════════════════════════════════════════════════
# _cfg — env-var overrides
# ══════════════════════════════════════════════════════════════════════════════

class TestCfg:
    def test_returns_defaults_when_no_env(self, monkeypatch):
        monkeypatch.delenv("RL_YFINANCE_RPS", raising=False)
        monkeypatch.delenv("RL_YFINANCE_BURST", raising=False)
        rps, burst = rl._cfg("yfinance")
        assert rps == 2.0 and burst == 12

    def test_env_overrides_rps(self, monkeypatch):
        monkeypatch.setenv("RL_YFINANCE_RPS", "5.0")
        rps, _ = rl._cfg("yfinance")
        assert rps == 5.0

    def test_env_overrides_burst(self, monkeypatch):
        monkeypatch.setenv("RL_YFINANCE_BURST", "99")
        _, burst = rl._cfg("yfinance")
        assert burst == 99

    def test_invalid_env_is_ignored(self, monkeypatch):
        monkeypatch.setenv("RL_YFINANCE_RPS", "not_a_number")
        rps, _ = rl._cfg("yfinance")
        assert rps == 2.0    # falls back to default

    def test_unknown_provider_has_fallback_defaults(self):
        rps, burst = rl._cfg("totally_unknown")
        assert rps == 2.0 and burst == 5


# ══════════════════════════════════════════════════════════════════════════════
# _Bucket.acquire — token bucket mechanics
# ══════════════════════════════════════════════════════════════════════════════

class TestBucketAcquire:
    def _bucket(self, rps=100.0, capacity=10):
        return rl._Bucket(rps=rps, capacity=float(capacity))

    def test_immediate_return_when_tokens_available(self):
        b = self._bucket()
        waited = b.acquire(weight=1.0)
        assert waited < 0.1

    def test_tokens_depleted_then_refilled(self):
        b = self._bucket(rps=1000.0, capacity=2)
        b.acquire(weight=2.0)    # drain
        # After a brief wait tokens refill
        time.sleep(0.01)
        waited = b.acquire(weight=1.0)
        assert waited < 0.1

    def test_throttle_events_zero_when_no_wait_needed(self):
        # Fresh full bucket: tokens available immediately → throttle_events stays 0
        b = self._bucket(rps=100.0, capacity=100)
        b.acquire(weight=1.0)
        assert b.throttle_events == 0

    def test_throttle_events_incremented_after_real_wait(self):
        # Drain fully, slow refill → next acquire definitely waits > 0.05s
        b = self._bucket(rps=0.5, capacity=1)
        b.tokens = 0.0
        b.acquire(weight=0.05, max_wait=1.0)  # waits ~0.1s for 0.05 tokens at 0.5/s
        assert b.throttle_events >= 1

    def test_fail_open_true_proceeds_after_max_wait(self):
        # rps=100, weight=1.0 → sleep_for = 1/100 = 0.01s → max(0.05, 0.01) = 0.05s/iter.
        # max_wait=0.12s → exits on the 3rd check (after ~0.10s of sleeping).
        b = self._bucket(rps=100.0, capacity=0)
        b.tokens = 0.0
        t0 = time.time()
        waited = b.acquire(weight=1.0, max_wait=0.12, fail_open=True)
        elapsed = time.time() - t0
        assert elapsed < 0.8   # well under 1s
        assert waited >= 0.0

    def test_fail_open_false_returns_minus_one_after_max_wait(self):
        b = self._bucket(rps=0.001, capacity=0)
        b.tokens = 0.0
        result = b.acquire(weight=1.0, max_wait=0.05, fail_open=False)
        assert result == -1.0

    def test_snapshot_has_expected_keys(self):
        b = self._bucket()
        s = b.snapshot()
        for key in ("rps", "capacity", "tokens_available", "waiters", "throttle_events", "last_wait_sec"):
            assert key in s

    def test_waiters_decremented_after_acquire(self):
        b = self._bucket()
        b.acquire(weight=1.0)
        assert b.waiters == 0


# ══════════════════════════════════════════════════════════════════════════════
# Public API: acquire / try_acquire / would_block / stats
# ══════════════════════════════════════════════════════════════════════════════

class TestPublicApi:
    def test_acquire_returns_float(self):
        result = rl.acquire("yfinance", weight=1.0)
        assert isinstance(result, float) and result >= 0.0

    def test_acquire_swallows_bucket_error(self, monkeypatch):
        monkeypatch.setattr(rl, "_get_bucket", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        assert rl.acquire("yfinance") == 0.0

    def test_try_acquire_returns_true_when_tokens_available(self):
        assert rl.try_acquire("market_data", weight=1.0) is True

    def test_try_acquire_returns_false_when_exhausted(self):
        b = rl._get_bucket("test_try")
        b.tokens = 0.0
        b.rps = 0.001
        result = rl.try_acquire("test_try", weight=1.0, max_wait=0.02)
        assert result is False

    def test_try_acquire_swallows_error(self, monkeypatch):
        monkeypatch.setattr(rl, "_get_bucket", lambda p: (_ for _ in ()).throw(RuntimeError()))
        assert rl.try_acquire("yfinance") is True   # fail open

    def test_would_block_false_when_tokens_available(self):
        rl._get_bucket("wb")   # ensure bucket exists with full tokens
        assert rl.would_block("wb", weight=1.0) is False

    def test_would_block_true_when_empty(self):
        b = rl._get_bucket("wb_empty")
        b.tokens = 0.0
        b.rps = 0.001
        assert rl.would_block("wb_empty", weight=1.0) is True

    def test_would_block_swallows_error(self, monkeypatch):
        monkeypatch.setattr(rl, "_get_bucket", lambda p: (_ for _ in ()).throw(RuntimeError()))
        assert rl.would_block("x") is False

    def test_stats_includes_created_buckets(self):
        rl.acquire("yfinance")
        rl.acquire("nse")
        s = rl.stats()
        assert "yfinance" in s and "nse" in s


# ══════════════════════════════════════════════════════════════════════════════
# Cooldown helpers
# ══════════════════════════════════════════════════════════════════════════════

class TestCooldowns:
    def test_not_in_cooldown_initially(self):
        assert rl.in_cooldown("yahoo") is False

    def test_in_cooldown_after_set(self):
        rl.set_cooldown("yahoo", seconds=30.0)
        assert rl.in_cooldown("yahoo") is True

    def test_not_in_cooldown_after_expiry(self):
        rl.set_cooldown("yahoo", seconds=0.01)
        time.sleep(0.02)
        assert rl.in_cooldown("yahoo") is False


# ══════════════════════════════════════════════════════════════════════════════
# suggested_timeout
# ══════════════════════════════════════════════════════════════════════════════

class TestSuggestedTimeout:
    def test_no_waiters_returns_base(self):
        rl._get_bucket("st")
        t = rl.suggested_timeout(10.0, "st")
        assert t == 10.0

    def test_many_waiters_widens_timeout(self):
        b = rl._get_bucket("st_busy")
        with b.lock:
            b.waiters = 6
        t = rl.suggested_timeout(10.0, "st_busy")
        assert t > 10.0

    def test_floor_is_respected(self):
        t = rl.suggested_timeout(0.0, "nonexistent", floor=5.0)
        assert t >= 5.0

    def test_swallows_error(self, monkeypatch):
        monkeypatch.setattr(rl, "_get_bucket", lambda p: (_ for _ in ()).throw(RuntimeError()))
        assert rl.suggested_timeout(10.0, "x") == 10.0


# ══════════════════════════════════════════════════════════════════════════════
# _yf_call_with_hard_timeout
# ══════════════════════════════════════════════════════════════════════════════

class _FakeCB:
    def __init__(self, allows=True):
        self._allows = allows
        self.successes = 0
        self.failures = []
    def allow(self): return self._allows
    def retry_after(self): return 5.0
    def record_success(self): self.successes += 1
    def record_failure(self, e=""): self.failures.append(e)


class TestYfCallWithHardTimeout:
    def _install_fake_cb(self, monkeypatch, allows=True):
        fake_cb = _FakeCB(allows=allows)
        fake_mod = types.ModuleType("circuit_breaker")
        fake_mod.get_breaker = lambda *a, **kw: fake_cb
        monkeypatch.setitem(sys.modules, "circuit_breaker", fake_mod)
        return fake_cb

    def test_calls_function_and_records_success(self, monkeypatch):
        fake_cb = self._install_fake_cb(monkeypatch)
        result = rl._yf_call_with_hard_timeout(lambda: 99)
        assert result == 99
        assert fake_cb.successes == 1

    def test_raises_runtime_error_when_circuit_open(self, monkeypatch):
        self._install_fake_cb(monkeypatch, allows=False)
        with pytest.raises(RuntimeError, match="circuit open"):
            rl._yf_call_with_hard_timeout(lambda: None)

    def test_records_failure_and_reraises_on_exception(self, monkeypatch):
        fake_cb = self._install_fake_cb(monkeypatch)
        with pytest.raises(ValueError, match="upstream error"):
            rl._yf_call_with_hard_timeout(lambda: (_ for _ in ()).throw(ValueError("upstream error")))
        assert "upstream error" in fake_cb.failures[0]

    def test_timeout_raises_timeout_error_and_records_failure(self, monkeypatch):
        fake_cb = self._install_fake_cb(monkeypatch)
        monkeypatch.setattr(rl, "YFINANCE_HARD_TIMEOUT_SEC", 0.01)
        with pytest.raises(TimeoutError, match="hard timeout"):
            rl._yf_call_with_hard_timeout(lambda: time.sleep(1.0))
        assert "hard timeout" in fake_cb.failures[0]

    def test_no_circuit_breaker_still_works(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "circuit_breaker", None)
        result = rl._yf_call_with_hard_timeout(lambda: "ok")
        assert result == "ok"


# ══════════════════════════════════════════════════════════════════════════════
# _breaker_allows_call
# ══════════════════════════════════════════════════════════════════════════════

class TestBreakerAllowsCall:
    def test_returns_true_when_no_circuit_breaker_module(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "circuit_breaker", None)
        assert rl._breaker_allows_call() is True

    def test_returns_true_when_circuit_closed(self, monkeypatch):
        fake_mod = types.ModuleType("circuit_breaker")
        fake_mod.get_breaker = lambda *a, **kw: _FakeCB(allows=True)
        monkeypatch.setitem(sys.modules, "circuit_breaker", fake_mod)
        assert rl._breaker_allows_call() is True

    def test_returns_false_and_logs_when_circuit_open(self, monkeypatch, caplog):
        import logging
        fake_mod = types.ModuleType("circuit_breaker")
        fake_mod.get_breaker = lambda *a, **kw: _FakeCB(allows=False)
        monkeypatch.setitem(sys.modules, "circuit_breaker", fake_mod)
        with caplog.at_level(logging.WARNING):
            result = rl._breaker_allows_call()
        assert result is False
        assert "circuit open" in caplog.text.lower()


# ══════════════════════════════════════════════════════════════════════════════
# patch_yfinance — idempotency and patching surface
# ══════════════════════════════════════════════════════════════════════════════

class TestPatchYfinance:
    def _make_stub_yf(self):
        """Build a minimal stub module that looks like yfinance."""
        yf = types.ModuleType("yfinance")
        _orig_download = lambda *a, **kw: "download_result"
        yf.download = _orig_download

        class _Ticker:
            def history(self, *a, **kw):
                return "history_result"
            @property
            def info(self):
                return {}

        yf.Ticker = _Ticker
        return yf, _orig_download

    def test_returns_false_when_yfinance_not_installed(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "yfinance", None)
        assert rl.patch_yfinance() is False

    def test_patches_and_returns_true(self, monkeypatch):
        yf, _ = self._make_stub_yf()
        monkeypatch.setitem(sys.modules, "yfinance", yf)
        result = rl.patch_yfinance()
        assert result is True
        assert rl._yf_patched is True

    def test_idempotent_second_call_returns_true_immediately(self, monkeypatch):
        yf, _ = self._make_stub_yf()
        monkeypatch.setitem(sys.modules, "yfinance", yf)
        rl.patch_yfinance()
        result = rl.patch_yfinance()
        assert result is True

    def test_patched_download_gates_on_circuit_breaker(self, monkeypatch):
        yf, orig = self._make_stub_yf()
        monkeypatch.setitem(sys.modules, "yfinance", yf)
        # Install an OPEN breaker so the patched download raises
        fake_mod = types.ModuleType("circuit_breaker")
        fake_mod.get_breaker = lambda *a, **kw: _FakeCB(allows=False)
        monkeypatch.setitem(sys.modules, "circuit_breaker", fake_mod)
        rl.patch_yfinance()
        with pytest.raises(RuntimeError, match="circuit open"):
            yf.download("RELIANCE.NS")

    def test_patched_history_runs_fn(self, monkeypatch):
        yf, _ = self._make_stub_yf()
        monkeypatch.setitem(sys.modules, "yfinance", yf)
        # No open breaker
        monkeypatch.setitem(sys.modules, "circuit_breaker", None)
        rl.patch_yfinance()
        ticker = yf.Ticker()
        # history is now patched — call it (hard timeout applies)
        result = ticker.history(period="1d")
        assert result == "history_result"
