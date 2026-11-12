"""E501: circuit breaker + numeric env settings.

1. circuit_breaker.py parsed CB_FAILURE_THRESHOLD / CB_HALF_OPEN_SUCCESS / CB_RECOVERY_TIMEOUT with bare int()/float() at import (a typo stopped the
   gateway), CB_REDIS_MIN_INTERVAL=x raised out of allow() / record_*() on every guarded call, CB_RECOVERY_TIMEOUT=nan|inf meant an open circuit never
   reached half_open, and state "open" loaded from the shared Redis state with no opened_at stayed open for ever (retry_after() answered 0).
2. main.py: MAX_UNIVERSE_PRICE / VALUE_BUY_THRESHOLD raised at import on a typo (and a blank MAX_UNIVERSE_PRICE hid MAX_STOCK_PRICE);
   _prediction_cache_ttl nan = cache off / inf = never expires; _nse_endpoint_env, _momentum_single_flight_cfg and _price_headroom_pct accepted inf
   (the last one overflowed in int(target * inf / 100)); _log_circuit_skip with nan / inf never logged its summary warning.
No fastapi needed (main.py functions lifted by AST; the real circuit_breaker is imported in a subprocess for the import-time cases).
"""
from __future__ import annotations

import ast
import math
import os
import subprocess
import sys
import time

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVICE not in sys.path:
    sys.path.insert(0, _SERVICE)

import circuit_breaker as cb  # noqa: E402

_MAIN = ast.parse(open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read())
_MAIN_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()


def _lift(name, extra=None):
    fn = next(n for n in _MAIN.body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {"math": math, "os": os, "time": time, "Any": object, "Optional": object, "logger": __import__("logging").getLogger("t")}
    ns.update(extra or {})
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), ns)
    return ns[name]


class _env:
    def __init__(self, **kv):
        self.kv, self.old = kv, {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.old[k] = os.environ.get(k)
            os.environ[k] = v

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _import_with(env):
    e = dict(os.environ)
    e.update(env)
    code = "import circuit_breaker as c;print(c.DEFAULT_FAILURE_THRESHOLD, c.DEFAULT_RECOVERY_TIMEOUT, c.DEFAULT_HALF_OPEN_SUCCESS)"
    return subprocess.run([sys.executable, "-c", code], cwd=_SERVICE, env=e, capture_output=True, text=True)


# ── 1. circuit_breaker ───────────────────────────────────────────────────────

def test_import_survives_bad_cb_env_and_uses_defaults():
    r = _import_with({"CB_FAILURE_THRESHOLD": "abc", "CB_RECOVERY_TIMEOUT": "nan", "CB_HALF_OPEN_SUCCESS": "abc"})
    assert r.returncode == 0, r.stderr[-200:]
    assert r.stdout.split() == ["12", "30.0", "2"]
    r = _import_with({"CB_RECOVERY_TIMEOUT": "inf"})
    assert r.returncode == 0 and r.stdout.split()[1] == "30.0"


def test_import_good_cb_env_unchanged():
    r = _import_with({"CB_FAILURE_THRESHOLD": "5", "CB_RECOVERY_TIMEOUT": "12.5", "CB_HALF_OPEN_SUCCESS": "3"})
    assert r.returncode == 0 and r.stdout.split() == ["5", "12.5", "3"]
    r = _import_with({"CB_FAILURE_THRESHOLD": " ", "CB_RECOVERY_TIMEOUT": ""})
    assert r.returncode == 0 and r.stdout.split() == ["12", "30.0", "2"]


def test_bad_redis_min_interval_does_not_raise_from_allow_or_record():
    with _env(CB_REDIS_SYNC="1", CB_REDIS_MIN_INTERVAL="x"):
        b = cb.CircuitBreaker("e501-iv")
        assert b.allow() is True
        b.record_failure("boom")
        b.record_success()


def test_constructor_non_finite_recovery_timeout_uses_default():
    for bad in (float("nan"), float("inf"), "abc", None):
        assert cb.CircuitBreaker("e501-rt", recovery_timeout=bad).recovery_timeout == cb.DEFAULT_RECOVERY_TIMEOUT
    assert cb.CircuitBreaker("e501-rt2", recovery_timeout=7).recovery_timeout == 7.0


def test_open_state_without_opened_at_recovers():
    b = cb.CircuitBreaker("e501-open", recovery_timeout=0.02)
    b._state = "open"   # what _load_remote_blocking leaves when the state key was found but opened_at was not
    assert b.allow() is False          # the recovery clock starts now
    time.sleep(0.06)
    assert b.allow() is True and b.state() == "half_open"


def test_normal_open_half_open_closed_cycle_unchanged():
    b = cb.CircuitBreaker("e501-nan", failure_threshold=2, recovery_timeout=0.02)
    b.record_failure("a")
    b.record_failure("b")
    assert b.state() == "open" and b.allow() is False
    time.sleep(0.06)
    assert b.state() == "half_open"
    b.record_success()
    b.record_success()
    assert b.state() == "closed"


# ── 2. main.py settings ──────────────────────────────────────────────────────

def test_env_float_safe():
    f = _lift("_env_float_safe")
    with _env(E501_A="abc", E501_B="", E501_C="nan", E501_D="inf", E501_E=" 250.5 ", E501_FB="500"):
        assert f("E501_A", 7.0) == 7.0 and f("E501_B", 7.0) == 7.0 and f("E501_C", 7.0) == 7.0 and f("E501_D", 7.0) == 7.0
        assert f("E501_E", 7.0) == 250.5 and f("E501_UNSET", 3.0) == 3.0
        assert f("E501_B", 0.0, fallback_name="E501_FB") == 500.0 and f("E501_E", 0.0, fallback_name="E501_FB") == 250.5
        assert f("E501_A", 0.0, fallback_name="E501_FB") == 0.0


def test_universe_price_constants_use_the_safe_helper():
    assert 'MAX_UNIVERSE_PRICE = _env_float_safe("MAX_UNIVERSE_PRICE", 0.0, fallback_name="MAX_STOCK_PRICE")' in _MAIN_SRC
    assert 'VALUE_BUY_THRESHOLD = _env_float_safe("VALUE_BUY_THRESHOLD", 2000.0)' in _MAIN_SRC


def test_prediction_cache_ttl():
    f = _lift("_prediction_cache_ttl")
    for raw, want in (("nan", 60.0), ("inf", 60.0), ("abc", 60.0), ("", 60.0), ("0", 0.0), ("-5", 0.0), ("15", 15.0)):
        with _env(PREDICTION_CACHE_TTL_SEC=raw):
            assert f() == want, raw


def test_nse_endpoint_env_headroom_and_momentum_wait():
    nse = _lift("_nse_endpoint_env")
    head = _lift("_price_headroom_pct")
    mom = _lift("_momentum_single_flight_cfg")
    with _env(E501_N="inf"):
        assert nse("E501_N", 30.0) == 30.0
    with _env(E501_N="12"):
        assert nse("E501_N", 30.0) == 12.0
    with _env(E501_N="-1"):
        assert nse("E501_N", 30.0) == 30.0
    for raw, want in (("inf", 20.0), ("nan", 20.0), ("-3", 20.0), ("abc", 20.0), ("0", 0.0), ("35", 35.0)):
        with _env(SCAN_UNIVERSE_PRICE_HEADROOM_PCT=raw):
            assert head() == want, raw
    for raw, want in (("inf", 45.0), ("nan", 45.0), ("-1", 45.0), ("abc", 45.0), ("0", 0.0), ("10", 10.0)):
        with _env(MOMENTUM_MOVERS_JOIN_WAIT_S=raw):
            assert mom()[1] == want, raw


def test_log_circuit_skip_non_finite_interval_still_warns():
    import logging
    seen = []

    class _L:
        def warning(self, *a, **k):
            seen.append("warn")

        def debug(self, *a, **k):
            seen.append("debug")

    for raw in ("nan", "inf"):
        f = _lift("_log_circuit_skip", {"logger": _L(), "_circuit_skip_log": {}})   # fresh per-name state for each case
        seen.clear()
        with _env(NEWS_CIRCUIT_LOG_EVERY_S=raw):
            f("news", "ABC", RuntimeError("open"))
        assert seen == ["warn"], raw
    logging.getLogger("t")


if __name__ == "__main__":
    fails = 0
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            try:
                _f()
                print("PASS", _n)
            except Exception as _e:  # noqa: BLE001
                fails += 1
                print("FAIL", _n, type(_e).__name__, str(_e)[:160])
    sys.exit(1 if fails else 0)
