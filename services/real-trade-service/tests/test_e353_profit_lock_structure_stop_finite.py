"""E353: the profit-lock and structure-step stop writes require a finite positive new stop (and profit lock re-checks 'only raise')."""
import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "exit_engine" / "exit.py"
_TEXT = SRC.read_text(encoding="utf-8")
_TREE = ast.parse(_TEXT)


def _evaluate_src():
    fn = next(n for n in ast.walk(_TREE) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "evaluate_mode")
    return ast.get_source_segment(_TEXT, fn)


def test_profit_lock_new_stop_is_validated_before_the_write():
    src = _evaluate_src()
    i_check = src.index('_usable_positive(_pl_v.get("new_stop")) is None')
    i_write = src.index('position.current_stop = float(_pl_v["new_stop"])')
    assert i_check < i_write
    assert "float(_pl_v[\"new_stop\"]) <= float(position.current_stop)" in src


def test_structure_new_stop_is_validated():
    src = _evaluate_src()
    assert '_usable_positive(_sx_v.get("new_stop")) is not None' in src
    assert '_sx_v.get("new_stop") is not None:' not in src
