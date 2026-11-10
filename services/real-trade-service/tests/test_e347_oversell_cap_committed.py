"""E347: the oversell sync caps qty_open to what the broker holds; that cap must be committed at once (a flush is lost on a later rollback)."""
import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "exit_engine" / "exit.py"


def _send_real_sell_node():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_send_real_sell")


def _calls(stmts, attr):
    return [n for s in stmts for n in ast.walk(s) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr]


def _cap_block():
    fn = _send_real_sell_node()
    for node in ast.walk(fn):
        if isinstance(node, ast.If):
            for i, st in enumerate(node.body):
                if (isinstance(st, ast.Assign) and isinstance(st.targets[0], ast.Attribute)
                        and st.targets[0].attr == "qty_open" and isinstance(st.value, ast.Name) and st.value.id == "broker_qty"):
                    return node.body, i
    raise AssertionError("cap block not found")


def test_cap_is_committed_inside_a_try_that_rolls_back():
    body, i = _cap_block()
    tries = [s for s in body[i + 1:] if isinstance(s, ast.Try)]
    assert tries, "the cap must be followed by a guarded commit"
    t = tries[0]
    assert _calls(t.body, "commit"), "commit missing"
    assert any(_calls(h.body, "rollback") for h in t.handlers), "a failed commit must roll back, not raise"


def test_commit_comes_before_the_qty_synced_alert():
    body, i = _cap_block()
    idx_try = next(j for j, s in enumerate(body) if j > i and isinstance(s, ast.Try))
    idx_notify = next(j for j, s in enumerate(body) if j > i and _calls([s], "notify_sync") or (j > i and isinstance(s, ast.Expr) and getattr(getattr(s.value, "func", None), "id", "") == "notify_sync"))
    assert idx_try < idx_notify
