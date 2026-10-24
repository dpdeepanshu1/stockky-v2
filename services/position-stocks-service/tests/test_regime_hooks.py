"""Market Weather step 2 (position-stocks-service): the risk multiplier is 1.0 by default, only ever reduces, is read at call time, and every
ScalpPosition carries the state stamp. Runs under pytest or plainly:  cd services/position-stocks-service && python3 tests/test_regime_hooks.py"""
from __future__ import annotations

import ast
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402


@contextmanager
def _set(name, value):
    old = getattr(config, name)
    setattr(config, name, value)
    try:
        yield
    finally:
        setattr(config, name, old)


def _load_mult():
    """The real helper from capital/ledger.py, executed on its own (the module needs sqlalchemy, which a bare checkout may not have)."""
    src = (ROOT / "capital" / "ledger.py").read_text(encoding="utf-8")
    fn = [n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_market_state_risk_mult"]
    assert len(fn) == 1
    ns = {"config": config}
    exec(compile(ast.Module(body=[fn[0]], type_ignores=[]), "ledger._market_state_risk_mult", "exec"), ns)
    return ns["_market_state_risk_mult"]


def test_defaults_change_nothing():
    assert config.MARKET_STATE_MODE == "shadow" and config.MARKET_STATE_RISK_MULT == 1.0
    assert _load_mult()() == 1.0


def test_multiplier_is_clamped_so_it_can_only_reduce():
    f = _load_mult()
    for raw, want in ((5.0, 1.0), (0.0, 0.1), (-3.0, 0.1), (float("nan"), 1.0), ("junk", 1.0), (None, 1.0), (0.37, 0.37)):
        with _set("MARKET_STATE_RISK_MULT", raw):
            assert f() == want, raw


def test_config_parsing_is_blank_safe_and_clamped():
    for env, mult in (("", 1.0), ("   ", 1.0), ("abc", 1.0), ("0.4", 0.4), ("9", 1.0), ("0", 0.1), ("-1", 0.1), ("nan", 1.0)):
        os.environ["MARKET_STATE_RISK_MULT"] = env
        try:
            assert config._ms_float("MARKET_STATE_RISK_MULT", 1.0, 0.1, 1.0) == mult, env
        finally:
            os.environ.pop("MARKET_STATE_RISK_MULT", None)
    for env, mode in (("", "shadow"), ("LIVE", "live"), (" off ", "off"), ("weird", "shadow")):
        os.environ["MARKET_STATE_MODE"] = env
        try:
            assert config._ms_mode() == mode, env
        finally:
            os.environ.pop("MARKET_STATE_MODE", None)


def test_ledger_applies_the_multiplier_once_in_the_sizing_formula():
    src = (ROOT / "capital" / "ledger.py").read_text(encoding="utf-8")
    assert src.count("* _market_state_risk_mult()") == 1
    m = re.search(r"risk_rupees = \((.*?)\n    \)", src, re.S)
    assert m and "config.RISK_PER_TRADE_PCT / 100.0" in m.group(1) and "_market_state_risk_mult()" in m.group(1)


def test_stamp_columns_models_migration_and_both_creation_sites():
    tree = ast.parse((ROOT / "models.py").read_text(encoding="utf-8"))
    cls = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ScalpPosition"][0]
    found = {st.targets[0].id: ast.unparse(st.value) for st in cls.body
             if isinstance(st, ast.Assign) and isinstance(st.targets[0], ast.Name) and st.targets[0].id in ("market_state", "profile_version")}
    assert found == {"market_state": "Column(String(16), nullable=True)", "profile_version": "Column(String(24), nullable=True)"}, found
    db = (ROOT / "db.py").read_text(encoding="utf-8")
    assert '("scalp_positions", "market_state", "VARCHAR2(16)", "VARCHAR(16)", None, None)' in db
    assert '("scalp_positions", "profile_version", "VARCHAR2(24)", "VARCHAR(24)", None, None)' in db
    entry = (ROOT / "orders" / "entry.py").read_text(encoding="utf-8")
    assert entry.count('market_state=_ms_current_profile()["market_state"]') == 2 and entry.count("ScalpPosition(") >= 2


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
