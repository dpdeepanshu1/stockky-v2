"""E503: numeric settings read at import time no longer stop market-data-service.  Runs with pytest or `python3 tests/test_e503_env_util.py`."""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVICE not in sys.path:
    sys.path.insert(0, _SERVICE)
import env_util as eu  # noqa: E402

_MODULES = ["angelone_scrip_master", "angelone_ws_feed", "bhavcopy", "kv_cache", "market_hours", "rate_limiter", "surprise_premarket", "angelone_client", "main"]


def test_helper_matrix():
    for raw, want in ((None, 7.0), ("", 7.0), ("abc", 7.0), ("nan", 7.0), ("inf", 7.0), ("2.5", 2.5), (" 3 ", 3.0)):
        old = os.environ.pop("E503_X", None)
        if raw is not None:
            os.environ["E503_X"] = raw
        try:
            assert eu.env_float_safe("E503_X", 7) == want, raw
        finally:
            os.environ.pop("E503_X", None)
            if old is not None:
                os.environ["E503_X"] = old
    assert eu.env_int_safe("E503_NOPE", 4) == 4


def _bare():
    out = []
    for fn in sorted(os.listdir(_SERVICE)):
        if not fn.endswith(".py") or fn == "env_util.py":
            continue
        src = open(os.path.join(_SERVICE, fn), encoding="utf-8").read()
        for n in ast.parse(src).body:
            seg = ast.get_source_segment(src, n) or ""
            if isinstance(n, (ast.Assign, ast.Expr)) and re.search(r"\b(float|int)\(", seg) and re.search(r"getenv|environ", seg):
                out.append(f"{fn}:{n.lineno}")
    return out


def test_no_bare_top_level_numeric_env_parse_is_left():
    assert _bare() == []


def test_modules_import_with_junk_and_blank_settings():
    checked = 0
    for mod in _MODULES:
        names = sorted(set(re.findall(r'env_(?:int|float)_safe\(\s*"(\w+)"', open(os.path.join(_SERVICE, mod + ".py"), encoding="utf-8").read())))
        if subprocess.run([sys.executable, "-c", f"import {mod}"], cwd=_SERVICE, capture_output=True, timeout=180).returncode != 0:
            continue   # third-party import missing here
        for val in ("abc", ""):
            env = dict(os.environ)
            env.update({n: val for n in names})
            r = subprocess.run([sys.executable, "-c", f"import {mod}"], cwd=_SERVICE, env=env, capture_output=True, text=True, timeout=180)
            assert r.returncode == 0, (mod, val, r.stderr[-300:])
        checked += 1
    assert checked >= 3


if __name__ == "__main__":
    bad = 0
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            try:
                _f()
                print("PASS", _n)
            except Exception as _e:  # noqa: BLE001
                bad += 1
                print("FAIL", _n, type(_e).__name__, str(_e)[:200])
    sys.exit(1 if bad else 0)
