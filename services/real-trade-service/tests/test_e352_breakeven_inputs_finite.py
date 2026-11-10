"""E352: the breakeven stop level and the profile's breakeven trigger / trail schedule are validated before use."""
import ast
import math
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "exit_engine" / "exit.py"
_TEXT = SRC.read_text(encoding="utf-8")
_TREE = ast.parse(_TEXT)


def _fn(name):
    return next(n for n in ast.walk(_TREE) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _ns(be_global=1.0):
    ns = {"math": math, "Optional": __import__("typing").Optional, "BREAKEVEN_ATR_TRIGGER": be_global}
    body = [_fn("_usable_positive"), _fn("_be_trigger_value"), _fn("_usable_schedule")]
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), "x", "exec"), ns)
    return ns


def test_be_trigger_bad_values_fall_back():
    f = _ns(1.5)["_be_trigger_value"]
    assert f(2.0) == 2.0
    for bad in (float("nan"), float("inf"), 0, -1, None, "x"):
        assert f(bad) == 1.5
    assert _ns(float("nan"))["_be_trigger_value"](0) == 1.0


def test_schedule_validation():
    f = _ns()["_usable_schedule"]
    good = [(2, 1.8), (99, 0.9)]
    assert f(good) == good
    for bad in ([], None, 5, [(2, float("nan"))], [(2, 0)], [(2, -1.0)], ["ab"]):
        assert f(bad) is None


def test_breakeven_level_write_is_guarded():
    src = ast.get_source_segment(_TEXT, _fn("evaluate_mode"))
    assert "_usable_positive(be_level) is not None and (position.current_stop is None" in src


def test_profile_values_are_validated_at_the_call_site():
    src = ast.get_source_segment(_TEXT, _fn("evaluate_mode"))
    assert "_be_trigger_value(_prof[\"breakeven_atr_trigger\"])" in src
    assert "_usable_schedule(_prof[\"trail_atr_schedule\"])" in src
