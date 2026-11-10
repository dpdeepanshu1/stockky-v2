"""E355: a failing rollback / closing commit in evaluate_mode must not abort the cycle for the other positions."""
import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "exit_engine" / "exit.py"
_TEXT = SRC.read_text(encoding="utf-8")
_TREE = ast.parse(_TEXT)
_FN = next(n for n in ast.walk(_TREE) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "evaluate_mode")
_SRC = ast.get_source_segment(_TEXT, _FN)


def test_handler_rollback_is_guarded():
    assert "except Exception as _pos_exc:\n            try:\n                db.rollback()" in _SRC


def test_hold_note_failure_rollback_is_guarded():
    assert "except Exception as _note_rb:" in _SRC


def test_closing_commit_is_guarded():
    assert "except Exception as _end_exc:" in _SRC
    assert "\n    db.commit()\n    tally = {" not in _SRC


def test_swing_backstop_failure_is_logged():
    assert "swing backstop check failed for" in _SRC
