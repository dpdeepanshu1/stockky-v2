"""group 76: source guard — numeric env settings must survive a blank value.

`int(os.getenv("X", "5"))` returns int("") -> ValueError at IMPORT time when X is set but empty (`X=` in a .env),
which takes the whole service down. Every numeric read must go through
`int(((os.getenv(NAME) or "").strip() or DEFAULT))` (or an equivalent helper).

Scans the non-test source of EVERY service (this file lives in api-gateway but guards the whole repo).
Run from services/api-gateway:  python3 -m pytest tests/test_env_numeric_sweep.py -q
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys

import pytest

SERVICES = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_ENV = {"os.getenv", "os.environ.get", "_os.getenv", "_os.environ.get"}


def _sources():
    for cur, dirs, files in os.walk(SERVICES):
        dirs[:] = [d for d in dirs if d not in {"tests", "__pycache__", "node_modules", ".git", ".venv", "venv", "site-packages", ".tox", "build", "dist"}]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(cur, f)


def _offenders():
    out = []
    for path in _sources():
        try:
            tree = ast.parse(open(path, encoding="utf-8", errors="ignore").read())
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and ast.unparse(n.func) in ("int", "float") and len(n.args) == 1 and not n.keywords:
                a = n.args[0]
                if isinstance(a, ast.Call) and ast.unparse(a.func) in _ENV and len(a.args) == 2:
                    out.append(f"{os.path.relpath(path, SERVICES)}:{n.lineno}")
    return out


def test_scan_covers_the_services():
    assert len(list(_sources())) > 200


def test_no_numeric_env_read_can_crash_on_a_blank_value():
    assert _offenders() == []


def _probe(code, cwd, env):
    full = dict(os.environ)
    full.update(env)
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=full, capture_output=True, text=True, timeout=120)
    return r


@pytest.mark.parametrize("blank", ["", "   "])
def test_real_trade_exit_engine_imports_with_blank_numeric_env(blank):
    rt = os.path.join(SERVICES, "real-trade-service")
    r = _probe("import exit_engine.exit as e; print(e.PARTIAL_EXIT_FRACTION, e.BREAKEVEN_ATR_TRIGGER)", rt,
               {"EXIT_PARTIAL_FRACTION": blank, "EXIT_BREAKEVEN_ATR_TRIGGER": blank})
    assert r.returncode == 0, r.stderr[-800:]
    assert r.stdout.split() == ["0.60", "1.0"] or r.stdout.split() == ["0.6", "1.0"], r.stdout


def test_explicit_numeric_values_still_apply_and_are_trimmed():
    rt = os.path.join(SERVICES, "real-trade-service")
    r = _probe("import exit_engine.exit as e; print(e.PARTIAL_EXIT_FRACTION)", rt, {"EXIT_PARTIAL_FRACTION": " 0.45 "})
    assert r.returncode == 0, r.stderr[-800:]
    assert r.stdout.strip() == "0.45"
