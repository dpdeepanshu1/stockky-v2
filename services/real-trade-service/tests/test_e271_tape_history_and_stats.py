"""E271: real-trade history rows carry the entry tape and the TAPE_TIGHTEN count; /flow/stats exists; the applied counter counts real moves."""
import ast
import json
import pathlib
from datetime import datetime, timedelta

from entry_engine import flow_gate as fg

MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"
EXIT = pathlib.Path(__file__).resolve().parents[1] / "exit_engine" / "exit.py"
T0 = datetime(2026, 10, 30, 4, 0, 0)


class _Col:
    def __getattr__(self, n): return self
    def __call__(self, *a, **k): return self
    def __eq__(self, o): return self
    def __ge__(self, o): return self
    def __le__(self, o): return self
    def __hash__(self): return 1


class _Q:
    def __init__(self, results): self._r = results
    def __getattr__(self, n): return lambda *a, **k: self
    def all(self): return self._r


class _DB:
    def __init__(self, events, decisions): self._qs = [events, decisions]
    def query(self, *a): return _Q(self._qs.pop(0))


class _M:
    TradePositionEvent = _Col()
    TradeDecision = _Col()


class _P:
    def __init__(self, id, symbol, opened_at, mode="REAL"): self.id, self.symbol, self.opened_at, self.mode = id, symbol, opened_at, mode


class _D:
    def __init__(self, symbol, created_at, flow_json, mode="REAL"): self.symbol, self.created_at, self.flow_json, self.mode = symbol, created_at, flow_json, mode


def _fn():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    f = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_tape_fields_for_positions")
    import logging
    import sys
    import types
    fake_sa = types.ModuleType("sqlalchemy")
    fake_sa.func = types.SimpleNamespace(count=lambda *a: _Col())
    ns = {"models": _M, "timedelta": timedelta, "logger": logging.getLogger("t")}
    exec(compile(ast.Module([f], []), "main.py", "exec"), ns)
    fn = ns["_tape_fields_for_positions"]

    def run(db, rows):
        old = sys.modules.get("sqlalchemy", None)      # the function imports sqlalchemy.func at call time: swap it only for the call
        sys.modules["sqlalchemy"] = fake_sa
        try:
            return fn(db, rows)
        finally:
            if old is None:
                sys.modules.pop("sqlalchemy", None)
            else:
                sys.modules["sqlalchemy"] = old
    return run


def test_history_joins_the_entry_tape_and_counts_tightens():
    f = _fn()
    rows = [_P(1, "AAA", T0), _P(2, "BBB", T0), _P(3, "CCC", T0)]
    decs = [_D("AAA", T0 - timedelta(seconds=20), json.dumps({"tape": {"against": 1, "reliable": True}})),
            _D("BBB", T0 - timedelta(hours=3), json.dumps({"tape": {"against": 2}})),          # too old to be this position's entry
            _D("CCC", T0 - timedelta(seconds=5), "not json")]
    out = f(_DB([(1, 2)], decs), rows)
    assert out[1]["tape"] == {"against": 1, "reliable": True} and out[1]["tightens"] == 2
    assert "tape" not in out.get(2, {}) and "tape" not in out.get(3, {})


def test_history_is_fail_open():
    f = _fn()
    class Boom:
        def query(self, *a): raise RuntimeError("db down")
    assert f(Boom(), [_P(1, "AAA", T0)]) == {}
    assert f(_DB([], []), []) == {}


def test_routes_and_row_fields_are_wired():
    src = MAIN.read_text(encoding="utf-8")
    assert '"tape": _tape_by_pos.get(p.id, {}).get("tape")' in src and '"tape_tightens": _tape_by_pos.get(p.id, {}).get("tightens", 0)' in src
    i = src.index('@app.get("/flow/stats")')
    seg = src[i:i + 1500]
    assert "require_admin" in seg and "_fg.stats()" in seg and "/internal/flow-status" in seg


def test_applied_counter_is_bumped_only_after_the_commit_in_step_5b():
    src = EXIT.read_text(encoding="utf-8")
    i = src.index("_fg.exit_applied()")
    assert "db.commit()" in src[i - 120:i] and src.index("TAPE_TIGHTEN") < i


def test_applied_counter():
    fg._reset_for_tests()
    assert fg.stats()["exit"]["applied"] == 0
    fg.exit_applied(); fg.exit_applied()
    assert fg.stats()["exit"]["applied"] == 2
    fg._reset_for_tests()
    assert fg.stats()["exit"]["applied"] == 0


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
