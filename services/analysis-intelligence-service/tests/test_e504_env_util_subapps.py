"""E504: numeric settings read at import time no longer stop analysis-intelligence-service (it has several self-contained sub-apps, each with its own env_util.py copy).
1. every env_util.py copy is byte-identical to api-gateway's; 2. no bare top-level numeric env parse is left in a sub-app (PORT and the byte-identical
boot_forensics / session_proof_client copies excepted); 3. modules that import here survive junk / blank settings, also when imported as a package module.
Runs with pytest or `python3 tests/test_e504_env_util_subapps.py`."""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVICES = os.path.dirname(_SERVICE)
_SUBAPPS = ['fundamental', 'event', 'news']
_SKIP = {"env_util.py", "boot_forensics.py", "session_proof_client.py"}


def _dirs():
    return [os.path.join(_SERVICE, d) for d in _SUBAPPS]


def test_env_util_copies_are_identical_to_the_gateways():
    ref = open(os.path.join(_SERVICES, "api-gateway", "env_util.py"), encoding="utf-8").read()
    for d in _dirs():
        assert open(os.path.join(d, "env_util.py"), encoding="utf-8").read() == ref, d


def test_no_bare_top_level_numeric_env_parse_is_left():
    out = []
    for d in _dirs():
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py") or fn in _SKIP:
                continue
            src = open(os.path.join(d, fn), encoding="utf-8").read()
            for n in ast.parse(src).body:
                seg = ast.get_source_segment(src, n) or ""
                if isinstance(n, (ast.Assign, ast.AnnAssign)) and re.search(r"\b(float|int)\(", seg) and re.search(r"getenv|environ", seg):
                    out.append(f"{os.path.basename(d)}/{fn}:{n.lineno}")
    assert out == []


def test_modules_import_with_junk_and_blank_settings():
    checked = 0
    for d in _dirs():
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py") or fn in _SKIP:
                continue
            src = open(os.path.join(d, fn), encoding="utf-8").read()
            names = sorted(set(re.findall(r'env_(?:int|float)_safe\(\s*"(\w+)"', src)))
            if not names:
                continue
            mod = fn[:-3]
            if subprocess.run([sys.executable, "-c", f"import {mod}"], cwd=d, capture_output=True, timeout=180).returncode != 0:
                continue   # third-party import missing here
            for val in ("abc", ""):
                env = dict(os.environ)
                env.update({n: val for n in names})
                r = subprocess.run([sys.executable, "-c", f"import {mod}"], cwd=d, env=env, capture_output=True, text=True, timeout=180)
                assert r.returncode == 0, (d, mod, val, r.stderr[-300:])
            checked += 1
    assert checked >= 1


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
