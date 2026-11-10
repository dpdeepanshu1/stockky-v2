"""E350: the ATR trail and the order-flow tape tighten never write a NaN / non-positive stop or target (NaN comparisons are False, so they slipped past 'only tighten')."""
import ast
import math
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "exit_engine" / "exit.py"
_TEXT = SRC.read_text(encoding="utf-8")
_TREE = ast.parse(_TEXT)


def _fn(name):
    return next(n for n in ast.walk(_TREE) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _usable():
    ns = {"math": math, "Optional": __import__("typing").Optional}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[_fn("_usable_positive")], type_ignores=[])), "x", "exec"), ns)
    return ns["_usable_positive"]


def _src():
    return ast.get_source_segment(_TEXT, _fn("evaluate_mode"))


def test_guard_rejects_what_the_old_comparisons_let_through():
    u = _usable()
    for bad in (float("nan"), float("inf"), 0, -1.5, None, "x"):
        assert u(bad) is None
    assert u(101.25) == 101.25


def test_trail_write_is_guarded():
    src = _src()
    i = src.index("trail_candidate > position.current_stop")
    assert "_usable_positive(trail_candidate)" in src[max(0, i - 160):i]


def test_tape_values_are_filtered_before_the_never_loosen_checks():
    src = _src()
    a = src.index('_n_stop, _n_tgt = _tx.get("new_stop")')
    b = src.index("# never loosen", a)
    seg = src[a:b]
    assert "_usable_positive(_n_stop)" in seg and "_usable_positive(_n_tgt)" in seg
