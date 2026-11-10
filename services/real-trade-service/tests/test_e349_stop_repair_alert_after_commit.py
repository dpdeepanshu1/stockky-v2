"""E349: _repair_non_finite_stop marks a position as warned only after its repair commit held, so a failed commit does not swallow the alert on the retry."""
import ast
import math
import pathlib
import types

SRC = pathlib.Path(__file__).resolve().parents[1] / "exit_engine" / "exit.py"
_TEXT = SRC.read_text(encoding="utf-8")
_FN = next(n for n in ast.walk(ast.parse(_TEXT)) if isinstance(n, ast.FunctionDef) and n.name == "_repair_non_finite_stop")


class _DB:
    def __init__(self, fail_commit=False, pos=None):
        self.fail_commit, self.rolled_back, self.pos = fail_commit, 0, pos

    def add(self, x):
        pass

    def commit(self):
        if self.fail_commit:
            raise RuntimeError("commit failed")

    def rollback(self):
        self.rolled_back += 1
        if self.pos is not None:
            self.pos.current_stop = float("nan")   # a real rollback reloads the committed (NaN) value


def _build(alerts):
    ns = {"math": math, "Optional": __import__("typing").Optional, "Session": object, "_BAD_STOP_WARNED": set(),
          "models": types.SimpleNamespace(TradePositionEvent=lambda **k: k),
          "logger": types.SimpleNamespace(critical=lambda *a, **k: None, error=lambda *a, **k: None, warning=lambda *a, **k: None),
          "notify_sync": lambda m: alerts.append(m),
          "_usable_positive": lambda v: v if isinstance(v, (int, float)) and math.isfinite(v) and v > 0 else None,
          "_emergency_risk_distance": lambda p: (5.0, "initial_stop_distance")}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[_FN], type_ignores=[])), "x", "exec"), ns)
    return ns


def _pos():
    return types.SimpleNamespace(id=1, symbol="ABC", current_stop=float("nan"), avg_entry_price=100.0)


def test_failed_commit_does_not_consume_the_alert():
    alerts = []
    ns = _build(alerts)
    pos = _pos()
    assert ns["_repair_non_finite_stop"](_DB(fail_commit=True, pos=pos), pos, "REAL") is False
    assert 1 not in ns["_BAD_STOP_WARNED"] and alerts == []
    assert ns["_repair_non_finite_stop"](_DB(), pos, "REAL") is True      # the retry succeeds ...
    assert len(alerts) == 1 and 1 in ns["_BAD_STOP_WARNED"]                 # ... and the alert goes out once


def test_successful_repair_alerts_once_only():
    alerts = []
    ns = _build(alerts)
    pos = _pos()
    assert ns["_repair_non_finite_stop"](_DB(), pos, "REAL") is True
    pos.current_stop = float("nan")
    ns["_repair_non_finite_stop"](_DB(), pos, "REAL")
    assert len(alerts) == 1


def test_unrebuildable_stop_alerts_once_only():
    alerts = []
    ns = _build(alerts)
    ns["_emergency_risk_distance"] = lambda p: (None, "none")
    pos = _pos()
    for _ in range(3):
        assert ns["_repair_non_finite_stop"](_DB(), pos, "REAL") is False
    assert len(alerts) == 1
