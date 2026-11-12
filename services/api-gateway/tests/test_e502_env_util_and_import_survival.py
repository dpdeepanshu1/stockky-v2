"""E502: numeric settings read at import time must not stop the gateway (or the decision / market-data circuit breakers).

1. env_util behaviour matrix.  2. Source guard: no module-level `int(` / `float(` over `getenv` / `environ` left in the gateway root modules, except the two
byte-identical cross-service copies (boot_forensics.py, session_proof_client.py) and PORT in the __main__ guard.  3. Import survival with junk / blank settings
(modules whose third-party imports are missing here are skipped).  4. decision / market-data circuit_breaker.py survive junk and NaN / inf settings.
Runs with pytest or `python3 tests/test_e502_env_util_and_import_survival.py`.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVICES = os.path.dirname(_SERVICE)
if _SERVICE not in sys.path:
    sys.path.insert(0, _SERVICE)

import env_util as eu  # noqa: E402

_PINNED_IDENTICAL = {"boot_forensics.py", "session_proof_client.py"}
_MODULES = ["buy_sniper", "data_feed", "hotpicks_schema", "hotpicks_store", "instant_scanner", "ipo_scanner", "ipo_schema", "kv_cache", "main",
            "rate_limiter", "refill_additional", "surprise_premarket", "surprise_scanner", "symbol_aliases"]


class _env:
    def __init__(self, **kv):
        self.kv, self.old = kv, {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_env_float_safe_matrix():
    for raw, want in ((None, 7.0), ("", 7.0), ("   ", 7.0), ("abc", 7.0), ("nan", 7.0), ("inf", 7.0), ("-inf", 7.0), ("2.5", 2.5), (" 3 ", 3.0),
                      ("0", 0.0), ("-2", -2.0), ("1e3", 1000.0)):
        with _env(E502_X=raw):
            r = eu.env_float_safe("E502_X", 7)
            assert r == want and isinstance(r, float), (raw, r)


def test_env_int_safe_matrix():
    for raw, want in ((None, 7), ("", 7), ("  ", 7), ("abc", 7), ("nan", 7), ("inf", 7), ("12", 12), (" 5 ", 5), ("0", 0), ("-3", -3), ("5.0", 5), ("5.9", 5)):
        with _env(E502_X=raw):
            r = eu.env_int_safe("E502_X", 7)
            assert r == want and isinstance(r, int), (raw, r)


def test_blank_override_keeps_a_site_that_treated_blank_differently_from_unset():
    with _env(E502_X=None):
        assert eu.env_int_safe("E502_X", 604800, blank=0) == 604800
    with _env(E502_X=""):
        assert eu.env_int_safe("E502_X", 604800, blank=0) == 0
        assert eu.env_float_safe("E502_X", 5, blank=0) == 0.0
    with _env(E502_X="abc"):
        assert eu.env_int_safe("E502_X", 604800, blank=0) == 604800
    with _env(E502_X="60"):
        assert eu.env_int_safe("E502_X", 604800, blank=0) == 60


def _bare_import_time_parses():
    out = []
    for fn in sorted(os.listdir(_SERVICE)):
        if not fn.endswith(".py") or fn == "env_util.py" or fn in _PINNED_IDENTICAL:
            continue
        src = open(os.path.join(_SERVICE, fn), encoding="utf-8").read()

        def walk(body, intry=False):
            for n in body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if isinstance(n, ast.ClassDef):
                    walk(n.body, intry)
                    continue
                if isinstance(n, ast.Try):
                    walk(n.body, True)
                    for h in n.handlers:
                        walk(h.body, intry)
                    walk(n.orelse, intry)
                    walk(n.finalbody, intry)
                    continue
                if isinstance(n, (ast.If, ast.For, ast.While, ast.With)):
                    walk(n.body, intry)
                    walk(getattr(n, "orelse", []), intry)
                    continue
                seg = ast.get_source_segment(src, n) or ""
                if not intry and re.search(r"\b(float|int)\(", seg) and re.search(r"getenv|environ", seg) and '"PORT"' not in seg:
                    out.append(f"{fn}:{n.lineno}")

        walk(ast.parse(src).body)
    return out


def test_no_bare_import_time_numeric_env_parse_is_left_in_the_gateway():
    assert _bare_import_time_parses() == []


def test_the_identical_cross_service_copies_were_left_alone():
    for fn in _PINNED_IDENTICAL:
        assert "env_util" not in open(os.path.join(_SERVICE, fn), encoding="utf-8").read(), fn


def _names(mod):
    src = open(os.path.join(_SERVICE, mod + ".py"), encoding="utf-8").read()
    return sorted(set(re.findall(r'env_(?:int|float)_safe\(\s*"(\w+)"', src)))


def _imports(mod, env, cwd=_SERVICE):
    return subprocess.run([sys.executable, "-c", f"import {mod}"], cwd=cwd, env=env, capture_output=True, text=True, timeout=180)


def test_every_changed_module_imports_with_junk_and_blank_settings():
    checked = 0
    for mod in _MODULES:
        names = _names(mod)
        assert names, mod
        if _imports(mod, dict(os.environ)).returncode != 0:
            print("SKIP (baseline import fails here):", mod)
            continue
        for val in ("abc", ""):
            env = dict(os.environ)
            for n in names:
                env[n] = val
            r = _imports(mod, env)
            assert r.returncode == 0, (mod, val, r.stderr[-300:])
        checked += 1
    assert checked >= 5


def test_decision_and_market_data_breakers_survive_junk_settings():
    for rel, rec in (("decision-prediction-service/decision", 45.0), ("market-data-service", 30.0)):
        cwd = os.path.join(_SERVICES, rel)
        for val in ("abc", "", "nan", "inf"):
            env = dict(os.environ, CB_FAILURE_THRESHOLD=val, CB_RECOVERY_TIMEOUT=val, CB_HALF_OPEN_SUCCESS=val, CB_REDIS_MIN_INTERVAL=val)
            code = "import circuit_breaker as c;print(c.DEFAULT_FAILURE_THRESHOLD,c.DEFAULT_RECOVERY_TIMEOUT,c.DEFAULT_HALF_OPEN_SUCCESS)"
            r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
            assert r.returncode == 0, (rel, val, r.stderr[-300:])
            assert r.stdout.split() == ["12", str(rec), "2"], (rel, val, r.stdout)


if __name__ == "__main__":
    fails = 0
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            try:
                _f()
                print("PASS", _n)
            except Exception as _e:  # noqa: BLE001
                fails += 1
                print("FAIL", _n, type(_e).__name__, str(_e)[:200])
    sys.exit(1 if fails else 0)
