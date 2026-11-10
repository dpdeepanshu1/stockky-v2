"""
E339 - exit_engine/exit.py evaluate_mode, two gaps on the stop path.

(a) A NaN / inf `current_stop` made `ltp <= stop` False forever: that position's stop never fired and nothing said so. The stop is now rebuilt
    (entry - stored risk distance, else the percent floor) with a STOP_REPAIRED event and one alert; with no usable distance only the alert
    goes out. None and finite stops are left alone.
(b) The early-warning day (`held_days == _early_warn and ltp <= entry`) wrote its HOLD row and then `continue`d, so the order-flow tape step (5b)
    never ran for a losing position all day. It now falls through; breakeven / trail / structure need ltp > entry, so only the tape step can
    act, and the cycle still counts one `held` and writes one HOLD row.

The real functions are pulled out of the source (the module needs sqlalchemy and the whole service to import).

Run from services/real-trade-service:  python -m pytest tests/test_e339_stop_nan_early_warn.py -q
"""
from __future__ import annotations

import ast
import logging
import math
import os
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "exit_engine", "exit.py"), encoding="utf-8").read()
NAN = float("nan")


def _load(sent):
    tree = ast.parse(SRC)
    want = {"_usable_positive", "_emergency_risk_distance", "_repair_non_finite_stop"}
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in want]
    assert {n.name for n in body} == want
    ns = {"math": math, "logger": logging.getLogger("e339"), "Optional": object, "Session": object,
          "EMERGENCY_NO_RISK_LOSS_PCT": 0.0, "_BAD_STOP_WARNED": set(),
          "notify_sync": lambda text: sent.append(text),
          "models": types.SimpleNamespace(TradePositionEvent=lambda **k: types.SimpleNamespace(**k))}
    exec(compile(ast.Module(body=body, type_ignores=[]), "exit_pulled", "exec"), ns)
    return ns


class _DB:
    def __init__(self):
        self.added, self.commits, self.rollbacks = [], 0, 0

    def add(self, o):
        self.added.append(o)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _pos(**kw):
    d = dict(id=1, symbol="TST", avg_entry_price=100.0, current_stop=NAN, initial_stop_distance=5.0)
    d.update(kw)
    return types.SimpleNamespace(**d)


def test_nan_stop_is_rebuilt_from_the_stored_distance_with_an_event_and_one_alert():
    sent = []
    ns = _load(sent)
    db, pos = _DB(), _pos()
    assert ns["_repair_non_finite_stop"](db, pos, "REAL") is True
    assert pos.current_stop == 95.0
    assert db.commits == 1 and db.added[0].event_type == "STOP_REPAIRED" and "nan" in db.added[0].detail
    assert len(sent) == 1 and "Stop repaired" in sent[0]
    pos.current_stop = float("inf")                       # a second break on the same position: repaired again, alert not repeated
    assert ns["_repair_non_finite_stop"](db, pos, "REAL") is True
    assert pos.current_stop == 95.0 and len(sent) == 1


def test_inf_stop_without_a_distance_falls_back_to_current_stop_then_alerts_only():
    sent = []
    ns = _load(sent)
    db, pos = _DB(), _pos(current_stop=float("inf"), initial_stop_distance=None)
    assert ns["_repair_non_finite_stop"](db, pos, "REAL") is False       # no distance, no pct floor: nothing to rebuild from
    assert math.isinf(pos.current_stop) and db.commits == 0
    assert len(sent) == 1 and "Broken stop" in sent[0]
    ns["_repair_non_finite_stop"](db, pos, "REAL")
    assert len(sent) == 1                                                 # once per position


def test_pct_floor_rebuilds_when_only_that_is_set():
    ns = _load([])
    ns["EMERGENCY_NO_RISK_LOSS_PCT"] = 4.0
    pos = _pos(initial_stop_distance=NAN)
    assert ns["_repair_non_finite_stop"](_DB(), pos, "REAL") is True
    assert pos.current_stop == 96.0


def test_none_finite_and_non_number_stops_are_left_alone():
    ns = _load([])
    for cs in (None, 95.0, 0.0, -1.0, "x", True):
        db, pos = _DB(), _pos(current_stop=cs)
        assert ns["_repair_non_finite_stop"](db, pos, "REAL") is False
        assert pos.current_stop == cs and db.commits == 0 and not db.added


def test_a_failing_commit_is_rolled_back_and_never_raises():
    ns = _load([])
    db = _DB()
    db.commit = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    assert ns["_repair_non_finite_stop"](db, _pos(), "REAL") is False
    assert db.rollbacks == 1


def test_evaluate_mode_repairs_the_stop_before_the_first_check_reads_it():
    i_rep = SRC.index("_repair_non_finite_stop(db, position, mode)   # E339")
    i_emerg = SRC.index("original_risk, _risk_src = _emergency_risk_distance(position)")
    i_stop = SRC.index("if position.current_stop is not None and ltp <= position.current_stop:")
    assert i_rep < i_emerg < i_stop


def test_early_warning_day_falls_through_to_the_tape_step_with_one_hold_row_and_one_held():
    i_warn = SRC.index("if held_days == _early_warn and ltp <= position.avg_entry_price:")
    i_tape = SRC.index("# ── 5b. E270")
    i_hold = SRC.index("# ── 6. Hold")
    raw = SRC[i_warn:SRC.index("# ── 3b. Daily market structure", i_warn)]
    warn_block = "\n".join(ln.split("#")[0] for ln in raw.splitlines())          # code only, comments dropped
    assert "_early_warned = True" in warn_block
    assert "continue" not in warn_block and "held += 1" not in warn_block
    hold_block = SRC[i_hold:i_hold + 600]
    assert "if not _early_warned:" in hold_block and "held += 1" in hold_block
    assert i_warn < i_tape < i_hold
