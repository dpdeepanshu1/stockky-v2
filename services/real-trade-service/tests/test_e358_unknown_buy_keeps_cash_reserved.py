"""E358: an UNKNOWN BUY (order may be live at Dhan) keeps its cash reserved for the rest of the cycle."""
import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "entry_engine" / "entry.py"
_TEXT = SRC.read_text(encoding="utf-8")
_FN = next(n for n in ast.walk(ast.parse(_TEXT)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "evaluate_mode")
_SRC = ast.get_source_segment(_TEXT, _FN)


def test_cash_back_is_zero_when_uncertain():
    assert "_cash_back = 0.0 if _uncertain else decision.proposed_qty * entry_price" in _SRC


def test_no_branch_subtracts_the_full_order_any_more():
    assert "reserved_cash -= decision.proposed_qty * entry_price" not in _SRC
    assert _SRC.count("reserved_cash -= _cash_back") == 3


def test_cash_back_is_set_after_uncertain_is_known_and_before_first_use():
    i_unc = _SRC.index("_uncertain = _broker_call_started and not _checked_ok")
    i_def = _SRC.index("_cash_back = 0.0 if _uncertain")
    i_use = _SRC.index("reserved_cash -= _cash_back")
    assert i_unc < i_def < i_use


def test_cash_back_is_defined_on_every_path_that_uses_it():
    # the three uses sit inside the `if _recovered_row is None:` block that defines it
    blk = _SRC[_SRC.index("if _recovered_row is None:\n                    # group335"):]
    assert blk.index("_cash_back = ") < blk.index("reserved_cash -= _cash_back")
