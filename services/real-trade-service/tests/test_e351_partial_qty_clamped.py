"""E351: the first-target partial quantity is clamped to 1..qty_open and a bad fraction falls back instead of overselling or raising."""
import ast
import math
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "exit_engine" / "exit.py"
_TEXT = SRC.read_text(encoding="utf-8")
_TREE = ast.parse(_TEXT)


def _fn(name):
    return next(n for n in ast.walk(_TREE) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _pq(global_frac=0.60):
    ns = {"math": math, "Optional": __import__("typing").Optional, "PARTIAL_EXIT_FRACTION": global_frac}
    body = [_fn("_usable_positive"), _fn("_partial_qty")]
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), "x", "exec"), ns)
    return ns["_partial_qty"]


def test_normal_fraction():
    assert _pq()(100, 0.6) == 60
    assert _pq()(100, 1.0) == 100


def test_never_less_than_one_never_more_than_open():
    pq = _pq()
    assert pq(1, 0.6) == 1
    assert pq(3, 0.1) == 1
    for f in (0.6, 1.0, 60, 1.5, float("nan"), float("inf"), 0, -1, None, "x"):
        for q in (1, 2, 7, 100):
            assert 1 <= pq(q, f) <= q


def test_bad_fraction_falls_back_to_the_global_then_to_sixty_percent():
    assert _pq(0.5)(100, 60) == 50
    assert _pq(0.5)(100, float("nan")) == 50
    assert _pq(float("nan"))(100, 60) == 60
    assert _pq(60)(100, None) == 60


def test_caller_uses_the_helper():
    src = ast.get_source_segment(_TEXT, _fn("evaluate_mode"))
    assert "_partial_qty(position.qty_open, _partial_frac)" in src
    assert "int(position.qty_open * _partial_frac)" not in src
