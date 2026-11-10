"""E354: the negative-event and swing-momentum stop raises require a finite positive new stop."""
import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "exit_engine" / "exit.py"
_TEXT = SRC.read_text(encoding="utf-8")
_TREE = ast.parse(_TEXT)
_FN = next(n for n in ast.walk(_TREE) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "evaluate_mode")
_SRC = ast.get_source_segment(_TEXT, _FN)


def test_event_tighten_validates_new_stop():
    assert '_usable_positive(_ee_v.get("new_stop")) is not None' in _SRC
    assert '_ee_v.get("new_stop") is not None' not in _SRC


def test_momentum_tighten_validates_new_stop():
    assert '_usable_positive(_sm_v.get("new_stop")) is not None' in _SRC
    assert '_sm_v.get("new_stop") is not None' not in _SRC
