"""group 75: source guard — no default-ON env flag may treat a blank value as "off".

`os.getenv("X", "true")` returns "" (not "true") when X is set but empty, and `"" .lower() == "true"` is False, so a
stray `X=` in a .env silently switched default-on safety switches off. Every such flag must go through
`((os.getenv(NAME) or "").strip() or DEFAULT)`.

Scans the non-test source of EVERY service (this file lives in api-gateway but guards the whole repo).
Run from services/api-gateway:  python3 -m pytest tests/test_env_flag_sweep.py -q
"""
from __future__ import annotations

import ast
import os

import pytest

SERVICES = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_TRUTHY = {"1", "true", "yes", "on", "y"}
_ENV_CALLS = {"os.getenv", "os.environ.get", "_os.getenv", "_os.environ.get"}
# Reads whose default is truthy but whose handling is already blank-safe or that are not boolean flags.
_ALLOWED = {
    "RL_RENAME_DISCOVERY",          # `not in ("0","false",...)`: a blank value is not in the off-list -> stays on
}
_NUMERIC_HINTS = ("int(", "float(", "_OVERFLOW", "POOL_SIZE")


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
            src = open(path, encoding="utf-8", errors="ignore").read()
            tree = ast.parse(src)
        except SyntaxError:
            continue
        lines = src.splitlines()
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and len(n.args) == 2 and not n.keywords):
                continue
            if ast.unparse(n.func) not in _ENV_CALLS:
                continue
            name, default = n.args
            if not (isinstance(name, ast.Constant) and isinstance(default, ast.Constant) and isinstance(default.value, str)):
                continue
            if default.value.strip().lower() not in _TRUTHY or name.value in _ALLOWED:
                continue
            line = lines[n.lineno - 1]
            if any(h in line for h in _NUMERIC_HINTS):
                continue
            out.append(f"{os.path.relpath(path, SERVICES)}:{n.lineno} {name.value}")
    return out


def test_scan_actually_covers_the_services():
    paths = list(_sources())
    assert len(paths) > 200, len(paths)
    assert any(p.endswith(os.path.join("real-trade-service", "config.py")) for p in paths)


def test_no_default_on_env_flag_reads_a_blank_value_as_off():
    assert _offenders() == []


@pytest.mark.parametrize("rel,name", [
    ("real-trade-service/config.py", "COST_MODEL_ENABLED"),
    ("real-trade-service/config.py", "EDIS_MORNING_CHECK_ENABLED"),
    ("real-trade-service/risk_engine/engine.py", "RISK_MAX_STOCK_PRICE_ADAPTIVE"),
    ("api-gateway/main.py", "WAKE_BEFORE_SCAN"),
    ("api-gateway/main.py", "DATA_FEED_SKIP_FUNDAMENTALS_AFTER_BULK"),
    ("notification-scheduler-service/scheduler/run_once.py", "PREFER_DATA_FEED"),
    ("decision-prediction-service/training/models.py", "FORCE_DB_POOLER"),
    ("decision-prediction-service/prediction/pred_train.py", "PRED_USE_SMOTE"),
])
def test_known_flags_use_the_strip_first_form(rel, name):
    src = open(os.path.join(SERVICES, rel), encoding="utf-8").read()
    assert f'((os.getenv("{name}") or "").strip() or' in src or f'(os.environ.get("{name}") or "").strip() or' in src \
        or f'((_os.environ.get("{name}") or "").strip() or' in src or f'((os.environ.get("{name}") or "").strip() or' in src, name


def test_risk_max_stock_price_is_parsed_blank_safely():
    src = open(os.path.join(SERVICES, "real-trade-service", "risk_engine", "engine.py"), encoding="utf-8").read()
    assert 'float(os.getenv("RISK_MAX_STOCK_PRICE", ' not in src
    assert 'os.getenv("RISK_MAX_STOCK_PRICE") is not None' not in src
