"""E506: numeric settings are range-checked, not only parse-checked.
E502-E505 made a typo / blank / nan / inf setting fall back to its default. A value that PARSES but breaks the code that uses it (DB_POOL_SIZE=0, a zero
or negative timeout or loop interval, a ratio above 1, a percentage above 100) still stalled a pool or a scan, or switched a guard off. env_int_safe /
env_float_safe now take lo= / hi= / positive=, and an out-of-range value falls back to the default with one log line per setting.
Same file in every service; it finds its own service folder.
1. env_util.py: every copy is identical to the gateway's and honours lo / hi / positive, rejects out-of-range text, logs once, and a valid value passes unchanged;
2. every env_*_safe site in the service that needs a bound (rule table below) carries it, unless already wrapped by max() / min(); every default satisfies its own bound;
3. the shared session_proof_client / boot_forensics copies are identical to the gateway's and fall back on 0 / negative / above-100 settings.
Rule table: a setting gets `positive` where 0 would stall something (pool, timeout, interval, batch / chunk size, window already over) and `lo=0` where 0 is a
deliberate "off" / "no wait" / "no limit" (checked in the code that reads it); signed settings and scores are exempt.
Runs with pytest or `python3 tests/test_e506_range_checked_env_settings.py`."""
from __future__ import annotations

import ast
import importlib.util
import logging
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = _HERE
while os.path.basename(os.path.dirname(_SERVICE)) != "services" and os.path.dirname(_SERVICE) != _SERVICE:
    _SERVICE = os.path.dirname(_SERVICE)
_SERVICES = os.path.dirname(_SERVICE)
_SKIPDIRS = {"tests", "__pycache__", "node_modules", ".git", "venv", ".venv", "site-packages", "archive"}
_FN = ("env_int_safe", "env_float_safe")
_SHARED = {"session_proof_client.py": [("SESSION_PROOF_CLIENT_TTL_S", "_TTL_S", 30.0, ("-1",)),
                                       ("SESSION_PROOF_CLIENT_TIMEOUT_S", "_TIMEOUT_S", 2.0, ("0", "-5"))],
           "boot_forensics.py": [("BOOT_FORENSICS_HEARTBEAT_S", "_HEARTBEAT_S", 15.0, ("0", "-1")),
                                 ("BOOT_FORENSICS_MEM_WARN_PCT", "_MEM_WARN_PCT", 85.0, ("0", "150"))]}

# ---- the rule table (identical in every service's copy of this test)
_F = dict(lo=0, hi=1)
_P = dict(lo=0, hi=100)
_POS = dict(positive=True)
_NN = dict(lo=0)
_EXACT = {
    # 0..1
    "ENTRY_REGIME_OVERRIDE_RISK_SCALE": _F, "EXIT_PARTIAL_FRACTION": _F, "EVENT_EXIT_MIN_STRENGTH": _F, "EVENT_EXIT_STRONG_STRENGTH": _F,
    "INTRADAY_EVENT_TRIGGER_MIN_STRENGTH": _F, "INTRADAY_EVENT_VETO_MIN_STRENGTH": _F, "SWING_EVENT_EXTEND_MIN_STRENGTH": _F,
    "OVERNIGHT_HOLD_MAX_RANGE_POS": _F, "PRED_DECISION_THRESHOLD": _F, "PRED_MIN_PRECISION": _F, "PRED_MIN_RECALL": _F,
    "REGIME_RULES_FULL_DEPTH": _F, "REGIME_RULES_SKIP_DEPTH": _F, "RL_INTERACTIVE_RESERVE": _F, "SURPRISE_ORB_ATR_FRACTION": _F,
    "VOLUME_SHOCK_TOD_MIN_FRACTION": _F, "RL_EPSILON": _F,
    # 0..100
    "ADAPTIVE_PERCENTILE": _P, "DEFAULT_RISK_PER_TRADE_PCT": _P, "DEFAULT_MAX_DAILY_LOSS_PCT": _P, "DEFAULT_MAX_PORTFOLIO_RISK_PCT": _P,
    "REAL_TRADE_CAPITAL_SHARE_PCT": _P, "OVERNIGHT_HOLD_MAX_EXPOSURE_PCT": _P, "OVERNIGHT_HOLD_MAX_SINGLE_SYMBOL_PCT": _P,
    "RISK_MAX_POSITION_CONCENTRATION_PCT": _P, "SIZE_LADDER_MIN_POSITIVE_DAYS_PCT": _P, "CANDIDATE_BASE_MIN_DELIVERY_PCT": _P,
    "CANDIDATE_DELIVERY_HIGH_QUALITY_PCT": _P, "GST_PCT": _P,
    # 0 would stall something (zero step / divisor, already-expired window, pool with no slot, time-stop on day 0)
    "BATCH_SIZE": _POS, "MAX_HISTORY_ROWS": _POS, "HYDRATOR_BATCH_HOURS": _POS, "IPO_FRESH_WINDOW_DAYS": _POS, "EXIT_MAX_HOLD_DAYS": _POS,
    "ENTRY_VALIDITY_MINUTES": _POS, "DEFAULT_STALE_DATA_SECONDS": _POS, "DHAN_TOKEN_LIFETIME_DAYS": _POS, "SCAN_UNIVERSE_TARGET": _POS,
    "SCAN_UNIVERSE_HARD_CAP": _POS, "PRED_LR": _POS,
    # E507: a breaker that opens after 0 failures / closes after 0 successes, a big-print multiple of 0 (every print is "big")
    "CB_FAILURE_THRESHOLD": _POS, "CB_HALF_OPEN_SUCCESS": _POS, "FLOW_BIG_PRINT_MULT": _POS,
    # 0 is a deliberate "off" / "no wait" / "no limit" / "halt" in the code that reads it (checked per setting): only a negative value is rejected
    "ANGELONE_FEED_COLD_INTERVAL_S": _NN, "BULK_YF_CHUNK_SLEEP": _NN, "BULK_YF_MAX_RETRIES_PER_CHUNK": _NN, "CANDIDATE_HISTORY_MAX_INFLIGHT": _NN,
    "DEFAULT_MAX_CONCURRENT_POSITIONS": _NN, "EVENT_INFLIGHT_WAIT_S": _NN, "SCAN_INTERVAL_MINUTES": _NN, "SURPRISE_FEED_MIN_FORCE_INTERVAL_SEC": _NN,
    "YFINANCE_MIN_INTERVAL_SEC": _NN, "PRED_SMOTE_K": _NN, "ENTRY_COMPOSITE_WEIGHT_CONVICTION": _NN, "ENTRY_CONVICTION_MAX_SCALE": _NN,
    "CHECK_MAX_SECONDS": _NN, "BHAVCOPY_DAY_WAIT_S": _NN, "CB_REDIS_MIN_INTERVAL": _NN,   # E507: 0 = persist / reload on every call
    # a signed value, or a deliberate negative: exempt
    "CANDIDATE_DOWNTREND_6M_PCT": {},
    "REFILL_CONCURRENCY": {}, "HYDRATOR_CONCURRENCY": {}, "NEON_KEEPALIVE_INTERVAL_SEC": {},   # the code already clamps these with max(1, ...) / max(60, ...): 0 means "the minimum"
    "BHAV_BULK_MAX_SEC": {},     # a negative budget is the documented "deadline already over: fetch nothing" (test_data_feed)
}
_EXEMPT_WORDS = {"CHANGE", "SCORE", "BONUS", "PENALTY", "CONVICTION", "BAR", "MIDPOINT", "EDGE", "SLOPE", "RVOL", "DOWNTREND"}
_ZERO_OK_WORDS = {"GAP", "DELAY", "SLEEP", "PAUSE", "COOLDOWN", "RETRIES", "POSITIONS", "HOURS", "BACKOFF", "SLACK"}
_POSITIVE_WORDS = {"INTERVAL", "TIMEOUT", "WORKERS", "CONCURRENCY", "CONCURRENT", "INFLIGHT", "CHUNK", "BYTES", "KEYS", "TOKENS", "LOOKBACK", "BATCHES",
                   "DEADLINE", "RECYCLE", "ESTIMATORS"}
_POSITIVE_SUFFIX = ("_POOL_SIZE", "_BATCH_SIZE", "_CHUNK_SIZE", "_BATCH_MAX", "_BATCH", "_MAX_DEPTH", "_BULK_BATCH")
def bounds_for(name, default):
    if name in _EXACT: return dict(_EXACT[name])
    words = set(name.split("_"))
    if words & _EXEMPT_WORDS: return {}
    if words & _ZERO_OK_WORDS: return dict(lo=0)
    if words & _POSITIVE_WORDS or name.endswith(_POSITIVE_SUFFIX): return dict(positive=True)
    return dict(lo=0)


def _fname(n):
    f = n.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def _py_files():
    for cur, dirs, files in os.walk(_SERVICE):
        dirs[:] = [d for d in dirs if d not in _SKIPDIRS]
        for f in sorted(files):
            if f.endswith(".py") and not f.startswith("test_") and f != "env_util.py":
                yield os.path.join(cur, f)


def _sites(path):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    parent = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parent[c] = p
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and _fname(n) in _FN and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
            default = None
            if len(n.args) > 1:
                try:
                    default = ast.literal_eval(n.args[1])
                except Exception:
                    default = "<expr>"
            wrapped, p = False, parent.get(n)
            while p is not None and not isinstance(p, ast.stmt):
                if isinstance(p, ast.Call) and _fname(p) in ("max", "min"):
                    wrapped = True
                p = parent.get(p)
            yield n.lineno, n.args[0].value, default, {k.arg for k in n.keywords}, wrapped


def _load_env_util(path):
    spec = importlib.util.spec_from_file_location("_e506_env_util_" + str(abs(hash(path))), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _env_utils():
    for cur, dirs, files in os.walk(_SERVICE):
        dirs[:] = [d for d in dirs if d not in _SKIPDIRS]
        if "env_util.py" in files:
            yield os.path.join(cur, "env_util.py")


def test_env_util_copies_are_identical_to_the_gateways():
    ref = open(os.path.join(_SERVICES, "api-gateway", "env_util.py"), encoding="utf-8").read()
    for p in _env_utils():
        assert open(p, encoding="utf-8").read() == ref, p


def test_env_util_range_arguments_behave_in_every_copy():
    for p in _env_utils():
        m = _load_env_util(p)
        I, F = m.env_int_safe, m.env_float_safe
        old = os.environ.get("E506_X")
        try:
            def chk(val, fn, want, *a, **k):
                os.environ["E506_X"] = val
                got = fn("E506_X", *a, **k)
                assert got == want and type(got) is (int if fn is I else float), (p, val, a, k, got, want)
            chk("0", I, 5, 5, positive=True)
            chk("-3", I, 5, 5, lo=0)
            chk("0", I, 0, 5, lo=0)
            chk("7", I, 7, 5, lo=0, hi=10)
            chk("11", I, 5, 5, lo=0, hi=10)
            chk("5.9", I, 5, 1, lo=0)
            chk("abc", I, 5, 5, positive=True)
            chk("0", F, 2.0, 2.0, positive=True)
            chk("0.0001", F, 0.0001, 2.0, positive=True)
            chk("500", F, 2.0, 2.0, lo=0, hi=100)
            chk("100", F, 100.0, 2.0, lo=0, hi=100)
            chk("nan", F, 2.0, 2.0, lo=0)
            chk("-1", F, -1.0, 2.0)            # no range argument: the E502 behaviour, unchanged
            chk("-1", I, -1, 5)
            chk("", F, 2.0, 2.0, positive=True)
            os.environ.pop("E506_X")
            assert F("E506_X", 2.0, positive=True) == 2.0 and I("E506_X", 5, lo=1) == 5
        finally:
            if old is None:
                os.environ.pop("E506_X", None)
            else:
                os.environ["E506_X"] = old


def test_a_rejected_setting_is_logged_once_and_a_valid_one_is_not():
    p = next(iter(_env_utils()), None)
    if p is None:
        return
    m = _load_env_util(p)
    records = []

    class _H(logging.Handler):
        def emit(self, r):
            records.append(r.getMessage())

    h = _H()
    m._log.addHandler(h)
    old_level = m._log.level
    m._log.setLevel(logging.WARNING)
    old = os.environ.get("E506_W")
    try:
        os.environ["E506_W"] = "0"
        for _ in range(3):
            assert m.env_int_safe("E506_W", 5, positive=True) == 5
        assert len(records) == 1 and "E506_W" in records[0], records
        os.environ["E506_W"] = "4"
        assert m.env_int_safe("E506_W", 5, positive=True) == 4
        assert len(records) == 1, records
    finally:
        m._log.removeHandler(h)
        m._log.setLevel(old_level)
        if old is None:
            os.environ.pop("E506_W", None)
        else:
            os.environ["E506_W"] = old


def test_every_setting_that_needs_a_bound_has_it():
    missing = []
    for p in _py_files():
        for line, name, default, kws, wrapped in _sites(p):
            if wrapped:
                continue
            b = bounds_for(name, default)
            need = [k for k in ("lo", "hi", "positive") if k in b and k not in kws]
            if need:
                missing.append(f"{os.path.relpath(p, _SERVICE)}:{line} {name} needs {need}")
    assert missing == [], missing


def test_every_default_satisfies_its_own_bound():
    bad = []
    for p in _py_files():
        for line, name, default, kws, wrapped in _sites(p):
            if wrapped or not isinstance(default, (int, float)) or isinstance(default, bool):
                continue
            b = bounds_for(name, default)
            if (b.get("positive") and not default > 0) or ("lo" in b and default < b["lo"]) or ("hi" in b and default > b["hi"]):
                bad.append(f"{os.path.relpath(p, _SERVICE)}:{line} {name} default {default} vs {b}")
    assert bad == [], bad


def test_shared_copies_are_identical_and_reject_out_of_range_settings():
    for cur, dirs, files in os.walk(_SERVICE):
        dirs[:] = [d for d in dirs if d not in _SKIPDIRS]
        for f in files:
            if f not in _SHARED:
                continue
            p = os.path.join(cur, f)
            ref = open(os.path.join(_SERVICES, "api-gateway", f), encoding="utf-8").read()
            assert open(p, encoding="utf-8").read() == ref, p
            specs = _SHARED[f]
            mod = f[:-3]
            probe = f"import {mod} as m; print(' '.join(repr(getattr(m, a)) for a in {[s[1] for s in specs]!r}))"
            for env_name, attr, default, bad_values in specs:
                for val in bad_values:
                    env = dict(os.environ)
                    env.update({s[0]: "" for s in specs})
                    env[env_name] = val
                    r = subprocess.run([sys.executable, "-c", probe], cwd=cur, env=env, capture_output=True, text=True, timeout=120)
                    assert r.returncode == 0, (p, env_name, val, r.stderr[-300:])
                    got = dict(zip([s[1] for s in specs], r.stdout.split()))
                    assert float(got[attr]) == default, (p, env_name, val, r.stdout)
            env = dict(os.environ)
            env.update({s[0]: "7.5" for s in specs})
            r = subprocess.run([sys.executable, "-c", probe], cwd=cur, env=env, capture_output=True, text=True, timeout=120)
            assert r.returncode == 0 and r.stdout.split() == ["7.5"] * len(specs), (p, r.stdout, r.stderr[-200:])


if __name__ == "__main__":
    bad = 0
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            try:
                _f()
                print("PASS", _n)
            except Exception as _e:   # noqa: BLE001
                bad += 1
                print("FAIL", _n, type(_e).__name__, str(_e)[:300])
    sys.exit(1 if bad else 0)
