"""E270: pure tape_exit.decide (real-trade copy, exit_engine/tape_exit.py)."""
from exit_engine import tape_exit as te

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
