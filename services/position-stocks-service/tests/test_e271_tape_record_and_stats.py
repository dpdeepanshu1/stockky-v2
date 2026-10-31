"""E271: the tape's stop / target moves are recorded on the trade (flow_json["tape_exit"]), counted, and the stats route exists."""
import ast
import json
import pathlib

import config
from orders import flow_gate as fg

MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"


class _Pos:
    def __init__(self, **k):
        self.id, self.symbol, self.status = 1, "ABC", "OPEN"
        self.entry_price, self.stop_price, self.target_price = 100.0, 98.0, 104.0
        self.dhan_super_order_id, self.plain_stop_order_id = "S1", None
        self.side, self.position_side, self.overnight_converted_to_cnc, self.stop_moved_to_breakeven = "LONG", "LONG", False, False
        self.__dict__.update(k)


class _DB:
    committed = 0
    def query(self, *a): return self
    def filter(self, *a): return self
    def all(self): return self.rows
    def commit(self): self.committed += 1
    def rollback(self): pass


def _run(pos, monkeypatch, decision, ltp=100.5, mode="live"):
    from orders import tape_hook as th
    monkeypatch.setattr(config, "FLOW_EXIT_MODE", mode)
    monkeypatch.setattr(th, "_ltp", lambda s: ltp)
    monkeypatch.setattr(th.flow_gate, "exit_check", lambda *a, **k: decision)
    monkeypatch.setattr(th.dhan_client, "modify_super_order", lambda db, **k: {})
    monkeypatch.setattr(th.dhan_client, "verify_stop_leg", lambda *a, **k: True)
    monkeypatch.setattr(th.side_mod, "is_short", lambda p: p.side == "SHORT")
    db = _DB(); db.rows = [pos]
    fg._reset_for_tests()
    return th.run_tape_exit(db)


def test_stamp_appends_keeps_the_entry_tape_and_caps_at_ten():
    j = json.dumps({"tape": {"against": 1}})
    for i in range(12):
        j = fg.stamp_tape_exit(j, {"n": i})
    d = json.loads(j)
    assert d["tape"] == {"against": 1} and len(d["tape_exit"]) == 10 and d["tape_exit"][-1] == {"n": 11} and d["tape_exit"][0] == {"n": 2}


def test_stamp_survives_empty_and_broken_input():
    assert json.loads(fg.stamp_tape_exit(None, {"a": 1}))["tape_exit"] == [{"a": 1}]
    assert json.loads(fg.stamp_tape_exit("", {"a": 1}))["tape_exit"] == [{"a": 1}]
    assert fg.stamp_tape_exit("not json", {"a": 1}) == "not json"          # unreadable JSON is left exactly as it was, never overwritten
    assert "tape_exit" in json.loads(fg.stamp_tape_exit("{}", {"x": object()}))   # an odd value is stringified, never raises


def test_hook_records_the_move_and_counts_it(monkeypatch):
    pos = _Pos(flow_json=json.dumps({"tape": {"against": 1, "reliable": True}}))
    n = _run(pos, monkeypatch, {"action": "TIGHTEN", "new_stop": 100.2, "new_target": 101.95, "reason": "TAPE_profit"})
    d = json.loads(pos.flow_json)
    m = d["tape_exit"][0]
    assert n == 1 and d["tape"]["against"] == 1
    assert m["old_stop"] == 98.0 and m["new_stop"] == 100.2 and m["old_target"] == 104.0 and m["new_target"] == 101.95 and m["ltp"] == 100.5 and m["plain"] is False
    assert fg.stats()["exit"]["applied"] == 1


def test_hook_records_a_plain_short_target_only_move(monkeypatch):
    pos = _Pos(side="SHORT", dhan_super_order_id=None, plain_stop_order_id="P1", stop_price=102.0, target_price=96.0)
    assert _run(pos, monkeypatch, {"action": "TIGHTEN", "new_stop": 99.75, "new_target": 98.05, "reason": "x"}, ltp=99.5) == 1
    m = json.loads(pos.flow_json)["tape_exit"][0]
    assert m["plain"] is True and m["old_stop"] == 102.0 and m["new_stop"] == 102.0 and m["new_target"] == 98.05


def test_log_mode_and_refused_moves_leave_no_record(monkeypatch):
    pos = _Pos()
    assert _run(pos, monkeypatch, {"action": "TIGHTEN", "new_stop": 100.2, "new_target": None, "reason": "x"}, mode="log") == 0
    assert getattr(pos, "flow_json", None) is None and fg.stats()["exit"]["applied"] == 0
    pos = _Pos()
    assert _run(pos, monkeypatch, {"action": "TIGHTEN", "new_stop": 97.0, "new_target": 105.0, "reason": "x"}) == 0
    assert getattr(pos, "flow_json", None) is None


def test_a_failing_stamp_never_undoes_the_move(monkeypatch):
    pos = _Pos()
    from orders import tape_hook as th
    monkeypatch.setattr(th.flow_gate, "stamp_tape_exit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    # _run re-patches exit_check only, so the stamp patch above stays in force
    monkeypatch.setattr(config, "FLOW_EXIT_MODE", "live")
    monkeypatch.setattr(th, "_ltp", lambda s: 100.5)
    monkeypatch.setattr(th.flow_gate, "exit_check", lambda *a, **k: {"action": "TIGHTEN", "new_stop": 100.2, "new_target": None, "reason": "x"})
    monkeypatch.setattr(th.dhan_client, "modify_super_order", lambda db, **k: {})
    monkeypatch.setattr(th.dhan_client, "verify_stop_leg", lambda *a, **k: True)
    monkeypatch.setattr(th.side_mod, "is_short", lambda p: False)
    db = _DB(); db.rows = [pos]
    assert th.run_tape_exit(db) == 1 and pos.stop_price == 100.2 and db.committed == 1


def _row_tape_fn():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_row_tape")
    ns = {}
    exec(compile(ast.Module([fn], []), "main.py", "exec"), ns)
    return ns["_row_tape"]


def test_row_tape_reads_both_keys_and_never_raises():
    f = _row_tape_fn()
    class R:  # noqa: N801
        flow_json = json.dumps({"tape": {"against": 2}, "tape_exit": [{"new_stop": 1}]})
    assert f(R) == ({"against": 2}, [{"new_stop": 1}])
    class E:  # noqa: N801
        flow_json = json.dumps({"order_flow": {"x": 1}})
    assert f(E) == (None, None)
    class B:  # noqa: N801
        flow_json = "not json"
    assert f(B) == (None, None)
    class N:  # noqa: N801
        flow_json = None
    assert f(N) == (None, None)


def test_history_and_stats_routes_are_wired():
    src = MAIN.read_text(encoding="utf-8")
    assert '"tape": _row_tape(r)[0]' in src and '"tape_exit": _row_tape(r)[1]' in src
    i = src.index('@app.get("/flow/stats")')
    assert "Depends(require_admin)" in src[i:i + 200] and "flow_gate.stats()" in src[i:i + 1200] and "/internal/flow-status" in src[i:i + 1200]


def _symbol_ok_fn():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_flow_symbol_ok")
    ns = {}
    exec(compile(ast.Module([fn], []), "main.py", "exec"), ns)
    return ns["_flow_symbol_ok"]


def test_flow_symbol_route_accepts_only_plain_symbols_and_is_admin_only():
    ok = _symbol_ok_fn()
    for good in ("SBIN", "m&m", "BAJAJ-AUTO", "NIFTY_50", "A" * 20):
        assert ok(good), good
    for bad in ("", "A" * 21, "../x", "SBIN/bars", "SBIN?x=1", "SB IN", "SBIN\n", None, 5, "SBIN#1", "SBIN%2F"):
        assert not ok(bad), bad
    src = MAIN.read_text(encoding="utf-8")
    i = src.index('@app.get("/flow/symbol/{symbol}")')
    seg = src[i:i + 1700]
    assert "require_admin" in seg and "_flow_symbol_ok(symbol)" in seg and "status_code=400" in seg and "/flow/{symbol.upper()}" in seg
