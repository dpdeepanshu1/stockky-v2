"""E270: pure tape_exit.decide + the position-stocks hook (orders/tape_hook.py)."""
import config
from orders import flow_gate as fg
from orders import tape_exit as te

CHK = {"delta": "against", "imbalance": "against", "profile": "against", "nodes": "neutral", "footprint": "neutral", "big_prints": "neutral"}


def tape(**k):
    return {"available": True, "reliable": True, "checks": dict(CHK), **k}


def test_long_profit_tightens_stop_and_trims_target_to_hvn():
    d = te.decide(side="LONG", entry=100, stop=98, target=104, ltp=100.5, tape=tape(hvn_above=102.0), tick=0.05)
    assert d["action"] == "TIGHTEN" and d["zone"] == "profit"
    assert 98 < d["new_stop"] < 100.5 and d["new_target"] == 101.95


def test_short_is_the_mirror():
    d = te.decide(side="SHORT", entry=100, stop=102, target=96, ltp=99.5, tape=tape(hvn_below=98.0), tick=0.05)
    assert d["action"] == "TIGHTEN" and 99.5 < d["new_stop"] < 102 and d["new_target"] == 98.05


def test_small_profit_holds():
    assert te.decide(side="LONG", entry=100, stop=98, target=104, ltp=100.2, tape=tape(), tick=0.05)["action"] == "HOLD"


def test_near_stop_zone():
    d = te.decide(side="LONG", entry=100, stop=98, target=104, ltp=99.0, tape=tape(), tick=0.05)
    assert d["zone"] == "near_stop" and d["new_stop"] > 98


def test_never_loosens_never_raises():
    d = te.decide(side="LONG", entry=100, stop=100.45, target=100.9, ltp=100.5, tape=tape(hvn_above=103.0), tick=0.05)
    assert d["new_stop"] is None and d["new_target"] is None


def test_unreliable_missing_or_weak_tape_holds():
    base = dict(side="LONG", entry=100, stop=98, target=104, ltp=100.5, tick=0.05)
    assert te.decide(tape={**tape(), "reliable": False}, **base)["reason"] == "tape_unreliable"
    assert te.decide(tape=None, **base)["reason"] == "no_tape"
    weak = tape(); weak["checks"] = {"delta": "against", "imbalance": "against", "profile": "neutral"}
    assert te.decide(tape=weak, **base)["reason"] == "not_enough_against"
    timing = tape(); timing["checks"] = {"profile": "against", "nodes": "against", "big_prints": "against"}
    assert te.decide(tape=timing, **base)["reason"] == "needs_delta_or_imbalance"


def test_bad_numbers_never_raise():
    assert te.decide(side="LONG", entry=None, stop="x", target=0, ltp=float("nan"), tape=tape(), tick=0)["action"] == "HOLD"


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
    calls = []
    monkeypatch.setattr(config, "FLOW_EXIT_MODE", mode)
    monkeypatch.setattr(th, "_ltp", lambda s: ltp)
    monkeypatch.setattr(th.flow_gate, "exit_check", lambda *a, **k: decision)
    monkeypatch.setattr(th.dhan_client, "modify_super_order", lambda db, **k: calls.append(k) or {})
    monkeypatch.setattr(th.dhan_client, "verify_stop_leg", lambda *a, **k: True)
    monkeypatch.setattr(th.side_mod, "is_short", lambda p: p.side == "SHORT")
    db = _DB(); db.rows = [pos]
    fg._reset_for_tests()
    return th.run_tape_exit(db), calls


def test_hook_super_order_moves_both_legs(monkeypatch):
    pos = _Pos()
    n, calls = _run(pos, monkeypatch, {"action": "TIGHTEN", "new_stop": 100.2, "new_target": 101.95, "reason": "TAPE_profit"})
    assert n == 1 and pos.stop_price == 100.2 and pos.target_price == 101.95
    assert {c["order_leg"] for c in calls} == {"STOP_LOSS_LEG", "TARGET_LEG"}


def test_hook_plain_short_never_touches_broker_stop(monkeypatch):
    pos = _Pos(side="SHORT", dhan_super_order_id=None, plain_stop_order_id="P1", stop_price=102.0, target_price=96.0)
    n, calls = _run(pos, monkeypatch, {"action": "TIGHTEN", "new_stop": 99.75, "new_target": 98.05, "reason": "x"}, ltp=99.5)
    assert n == 1 and calls == [] and pos.stop_price == 102.0 and pos.target_price == 98.05


def test_hook_log_mode_changes_nothing(monkeypatch):
    pos = _Pos()
    n, calls = _run(pos, monkeypatch, {"action": "TIGHTEN", "new_stop": 100.2, "new_target": None, "reason": "x"}, mode="log")
    assert n == 0 and calls == [] and pos.stop_price == 98.0


def test_hook_refuses_a_loosening_decision(monkeypatch):
    pos = _Pos()
    n, calls = _run(pos, monkeypatch, {"action": "TIGHTEN", "new_stop": 97.0, "new_target": 105.0, "reason": "x"})
    assert n == 0 and calls == [] and pos.stop_price == 98.0 and pos.target_price == 104.0


def test_exit_check_off_returns_none(monkeypatch):
    monkeypatch.setattr(config, "FLOW_EXIT_MODE", "off")
    assert fg.exit_check("ABC", "LONG", entry=100, stop=98, target=104, ltp=100.5) is None
