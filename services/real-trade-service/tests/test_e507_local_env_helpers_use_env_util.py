"""E507: the settings that used a service's own private env helper are now read through env_util, with a range.
E502-E506 moved most numeric settings onto `env_int_safe` / `env_float_safe`. About 60 reads in 13 files still used a local helper (`_env_float`, `_env_num`,
`_env_float_or`, `_envf`, `_envi`, the circuit breaker's `_env_int` / `_env_float`) that guarded against a typo but not against a value that parses and breaks the code.
1. none of those helpers is defined any more in the files that had them (the shared `return_sanity.py` copies keep a deliberately self-contained one);
2. every setting in the table below is read through env_util with exactly the range written here (a value that is clamped by max() / min() on the same line stays without one);
3. a circuit breaker given 0 / negative / text settings loads the same constants as with nothing set, and a valid value passes unchanged;
4. `return_sanity.py` rejects a zero / negative / text CORPORATE_ACTION_JUMP_THRESHOLD (the two copies stay identical).
Same file in every service it applies to; it only checks the files that exist in its own service.
Runs with pytest or `python3 tests/test_e507_local_env_helpers_use_env_util.py`."""
from __future__ import annotations

import ast
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = _HERE
while os.path.basename(os.path.dirname(_SERVICE)) != "services" and os.path.dirname(_SERVICE) != _SERVICE:
    _SERVICE = os.path.dirname(_SERVICE)
_SERVICES = os.path.dirname(_SERVICE)
_ROOT = os.path.dirname(_SERVICES)
_SVC = os.path.basename(_SERVICE)

_POS = {"positive": True}
_NN = {"lo": 0}
_NONE = {}
_CB = {"CB_FAILURE_THRESHOLD": _POS, "CB_RECOVERY_TIMEOUT": _POS, "CB_HALF_OPEN_SUCCESS": _POS}

# path (relative to services/) -> {setting: expected keyword arguments}
_TABLE = {
    "api-gateway/circuit_breaker.py": dict(_CB, CB_REDIS_MIN_INTERVAL=_NN),
    "api-gateway/loop_watchdog.py": {"GATEWAY_LOOP_STALL_WARN_S": _NONE, "GATEWAY_LOOP_STALL_LOG_EVERY_S": _NONE},   # wrapped in max(0.1, ...)
    "market-data-service/circuit_breaker.py": dict(_CB),
    "market-data-service/order_flow_tape.py": {
        "FLOW_TAPE_MAX_SYMBOLS": _NONE, "FLOW_MIN_INTERVALS": _NN, "FLOW_MIN_TAPE_MINUTES": _NN,
        "FLOW_MIN_COVERAGE_PCT": {"lo": 0, "hi": 100}, "FLOW_BIG_PRINT_MULT": _POS, "FLOW_BIG_PRINT_MIN_VALUE_RS": _NN, "FLOW_GATE_MIN_AGAINST": _NONE},
    "market-data-service/session_proof.py": {"SESSION_PROOF_MIN_MOVES": _NONE, "SESSION_PROOF_MIN_SYMBOLS": _NONE, "SESSION_NIFTY_MIN_OBS": _NONE, "SESSION_NIFTY_PROBE_S": _NONE},
    "decision-prediction-service/decision/circuit_breaker.py": dict(_CB, CB_REDIS_MIN_INTERVAL=_NN),
    "real-trade-service/candidate_engine/candidates.py": {
        "CANDIDATE_52W_FRESH_HIGH_PCT": {"lo": 0, "hi": 100}, "CANDIDATE_52W_FRESH_HIGH_DAYS": _NN, "CANDIDATE_52W_FRESH_HIGH_VOL_RATIO": _NN},
    "real-trade-service/regime_adaptive.py": {
        "REGIME_ADAPTIVE_MAX_AGE_H": _NONE, "REGIME_ADAPTIVE_HISTORY_TIMEOUT_S": _POS, "REGIME_ADAPTIVE_RESULT_DAYS": _POS,
        "REGIME_ADAPTIVE_MIN_TRADES": _NN, "REGIME_ADAPTIVE_REFRESH_S": _NONE},
    "real-trade-service/entry_engine/orb_bars.py": {"ORB_BARS_REFRESH_S": _POS, "ORB_BARS_MAX_LAG_S": _POS, "ORB_BARS_TIMEOUT_S": _POS, "ORB_BARS_MAX_INFLIGHT": _POS},
    "real-trade-service/swing/ranker.py": {"SWING_RANK_MAX_SYMBOLS": _POS, "SWING_RANK_WORKERS": _POS, "SWING_RANK_TIMEOUT_S": _POS},
    "real-trade-service/market_feed/feed.py": {
        "FEED_PRIORITY_BULK_MAX_AGE_S": _POS, "FEED_PRIORITY_BULK_TIMEOUT_S": _POS, "FEED_PRIORITY_BULK_COOLDOWN_S": _NN, "FEED_PRIORITY_SHARE_S": _NN,
        "FEED_PRIORITY_HEDGE_S": _NN, "FEED_PRIORITY_STALE_FALLBACK_S": _NN, "FEED_BACKPRESSURE_S": _NN, "FEED_LEFTOVER_MAX": _NN,
        "FEED_LEFTOVER_STALE_S": _NN, "FEED_ATR_WARM_REFRESH_PER_MIN": _NN, "FEED_DISPLAY_BULK_MIN_SYMBOLS": _NONE,
        "FEED_DISPLAY_BULK_MAX_AGE_S": _POS, "FEED_DISPLAY_LEFTOVER_MAX": _NN},
    "real-trade-service/watchlist_engine/sources.py": {
        "WATCHLIST_TIER1_WARMUP_RETRIES": _NN, "WATCHLIST_TIER1_WARMUP_WAIT_S": _NN, "WATCHLIST_TIER1_WARMUP_MIN_GAP_S": _NN, "WATCHLIST_TIER1_SNAPSHOT_MAX_AGE_H": _NN},
}
_LEGACY = {"_env_float", "_env_num", "_env_float_or", "_envf", "_envi", "_env_int"}
_FN = {"env_int_safe", "env_float_safe"}


def _mine():
    return {p: t for p, t in _TABLE.items() if p.split("/", 1)[0] == _SVC and os.path.exists(os.path.join(_SERVICES, p))}


def _tree(path):
    return ast.parse(open(path, encoding="utf-8").read())


def test_the_local_helpers_are_gone():
    left = []
    for rel in _mine():
        for n in ast.walk(_tree(os.path.join(_SERVICES, rel))):
            if isinstance(n, ast.FunctionDef) and n.name in _LEGACY:
                left.append(f"{rel}:{n.lineno} def {n.name}")
    assert left == [], left


def test_every_setting_is_read_through_env_util_with_its_range():
    bad = []
    for rel, want in _mine().items():
        got = {}
        for n in ast.walk(_tree(os.path.join(_SERVICES, rel))):
            if isinstance(n, ast.Call):
                f = n.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if name in _FN and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
                    kws = {k.arg: ast.literal_eval(k.value) for k in n.keywords if k.arg in ("lo", "hi", "positive")}
                    got.setdefault(n.args[0].value, []).append(kws)
        for setting, kws in want.items():
            if setting not in got:
                bad.append(f"{rel}: {setting} is not read through env_util")
            elif any(g != kws for g in got[setting]):
                bad.append(f"{rel}: {setting} has {got[setting]}, wanted {kws}")
    assert bad == [], bad


def _run(path, code, env):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("CB_", "CORPORATE_"))}
    e.update(env)
    e["PYTHONPATH"] = os.path.dirname(path)
    e["PYTHONDONTWRITEBYTECODE"] = "1"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=e, cwd=os.path.dirname(path), timeout=60)
    assert r.returncode == 0, r.stderr[-400:]
    return r.stdout.split()


def test_circuit_breaker_ignores_out_of_range_settings_and_keeps_valid_ones():
    code = "import circuit_breaker as c; print(c.DEFAULT_FAILURE_THRESHOLD, c.DEFAULT_RECOVERY_TIMEOUT, c.DEFAULT_HALF_OPEN_SUCCESS)"
    for rel in _mine():
        if not rel.endswith("circuit_breaker.py"):
            continue
        path = os.path.join(_SERVICES, rel)
        clean = _run(path, code, {})
        for bad in ({"CB_FAILURE_THRESHOLD": "0", "CB_RECOVERY_TIMEOUT": "0", "CB_HALF_OPEN_SUCCESS": "0"},
                    {"CB_FAILURE_THRESHOLD": "-3", "CB_RECOVERY_TIMEOUT": "-1", "CB_HALF_OPEN_SUCCESS": "-1"},
                    {"CB_FAILURE_THRESHOLD": "abc", "CB_RECOVERY_TIMEOUT": "nan", "CB_HALF_OPEN_SUCCESS": "x"}):
            assert _run(path, code, bad) == clean, (rel, bad)
        assert _run(path, code, {"CB_FAILURE_THRESHOLD": "5", "CB_RECOVERY_TIMEOUT": "12.5", "CB_HALF_OPEN_SUCCESS": "3"}) == ["5", "12.5", "3"], rel


def test_return_sanity_rejects_a_zero_or_negative_jump_threshold():
    paths = [p for p in (os.path.join(_SERVICE, "return_sanity.py"), os.path.join(_ROOT, "shared", "return_sanity.py")) if os.path.exists(p)]
    if _SVC != "real-trade-service" or not paths:
        return
    code = "import return_sanity as r; print(r.CORPORATE_ACTION_JUMP_THRESHOLD)"
    for path in paths:
        for v in ("0", "-5", "abc", "nan", ""):
            assert _run(path, code, {"CORPORATE_ACTION_JUMP_THRESHOLD": v}) == ["30.0"], (path, v)
        assert _run(path, code, {"CORPORATE_ACTION_JUMP_THRESHOLD": "12.5"}) == ["12.5"], path
    if len(paths) == 2:
        assert open(paths[0], encoding="utf-8").read() == open(paths[1], encoding="utf-8").read()


if __name__ == "__main__":
    fails = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            try:
                v(); print("ok  ", k)
            except Exception as e:
                fails += 1; print("FAIL", k, repr(e)[:300])
    sys.exit(1 if fails else 0)
