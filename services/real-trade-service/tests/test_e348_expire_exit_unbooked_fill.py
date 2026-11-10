"""E348: an exit LIMIT order with shares traded at Dhan that are not booked yet is left for reconcile, not marked EXPIRED."""
import ast
import pathlib
import types

SRC = pathlib.Path(__file__).resolve().parents[1] / "exit_engine" / "exit.py"
_TREE = ast.parse(SRC.read_text(encoding="utf-8"))


def _fn(name):
    return next(n for n in ast.walk(_TREE) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _helper(rows=None, boom=False):
    mod = ast.Module(body=[_fn("_broker_filled_qty")], type_ignores=[])
    ns = {"Optional": __import__("typing").Optional, "Session": object}

    def get_order_list(db):
        if boom:
            raise RuntimeError("down")
        return rows

    ns["dhan_client"] = types.SimpleNamespace(get_order_list=get_order_list)
    exec(compile(ast.fix_missing_locations(mod), "x", "exec"), ns)
    return ns["_broker_filled_qty"]


def test_reads_filled_qty():
    assert _helper([{"orderId": "9", "filledQty": 4}])(None, "9") == 4


def test_falls_back_to_quantity_minus_remaining():
    assert _helper([{"orderId": "9", "quantity": 10, "remainingQuantity": 7}])(None, "9") == 3


def test_none_when_unreadable_missing_or_garbled():
    assert _helper(boom=True)(None, "9") is None
    assert _helper([{"orderId": "1", "filledQty": 2}])(None, "9") is None
    assert _helper([{"orderId": "9", "filledQty": "nan"}])(None, "9") is None
    assert _helper([{"orderId": "9", "filledQty": "inf"}])(None, "9") is None


def test_expire_checks_traded_qty_before_marking_expired():
    src = ast.get_source_segment(SRC.read_text(encoding="utf-8"), _fn("expire_stale_exit_orders"))
    assert src.index("_broker_filled_qty(") < src.index('order.status     = "EXPIRED"')
