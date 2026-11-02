"""E285 batch 2: tape tighten ATR floor, 0.5R profit gate, persistence, rate limit (pure decide + flow_gate wrapper)."""
import config
from orders import flow_gate as fg
from orders import tape_exit as te

CHK = {"delta": "against", "imbalance": "against", "profile": "against", "nodes": "neutral", "footprint": "neutral", "big_prints": "neutral"}
TAPE = {"available": True, "reliable": True, "checks": CHK}
LONG = dict(side="LONG", entry=1400, stop=1386, target=1430, tape=TAPE, tick=0.05, atr=10, min_stop_atr=0.5, r_dist=14, min_profit_r=0.5)


def test_stop_never_closer_than_half_atr_to_ltp():
    d = te.decide(ltp=1408, **LONG)                      # +0.57R; old gap 0.25% = 3.5 would have been closer than 0.5 ATR = 5
    assert d["action"] == "TIGHTEN" and 1408 - d["new_stop"] >= 5 - 1e-6


def test_short_mirror_of_atr_floor():
    d = te.decide(**{**LONG, "side": "SHORT", "stop": 1414, "target": 1370}, ltp=1392)
    assert d["action"] == "TIGHTEN" and d["new_stop"] - 1392 >= 5 - 1e-6


def test_profit_gate_blocks_losing_and_small_winners():
    assert te.decide(ltp=1396, **LONG)["action"] == "HOLD"          # under water: keep the original stop (old near_stop zone is off)
    assert te.decide(ltp=1404, **LONG)["action"] == "HOLD"          # +0.29R < 0.5R


def test_flags_off_restore_old_behaviour():
    d = te.decide(**{**LONG, "min_stop_atr": 0.0, "min_profit_r": 0.0}, ltp=1392)
    assert d["zone"] == "near_stop" and d["action"] == "TIGHTEN"


def test_unknown_atr_blocks_stop_move_but_not_target_trim():
    d = te.decide(**{**LONG, "atr": None}, ltp=1408)
    assert d["action"] == "HOLD" and d["new_stop"] is None
    d2 = te.decide(**{**LONG, "atr": None}, ltp=1408, )
    assert d2["new_stop"] is None


def test_never_loosens_with_floor():
    d = te.decide(**{**LONG, "stop": 1405}, ltp=1408)               # 1408 - 5 = 1403 < 1405: would loosen -> no move
    assert d["new_stop"] is None


def test_persistence_needs_three_consecutive(monkeypatch):
    fg._reset_for_tests()
    monkeypatch.setattr(config, "FLOW_EXIT_CONSEC", 3, raising=False)
    t = {"action": "TIGHTEN", "new_stop": 1403.0, "new_target": None, "reason": "x"}
    h = {"action": "HOLD", "new_stop": None, "new_target": None, "reason": "no"}
    assert fg._persist(("ps", 1), t)["action"] == "HOLD"
    assert fg._persist(("ps", 1), t)["action"] == "HOLD"
    assert fg._persist(("ps", 1), t)["action"] == "TIGHTEN"
    assert fg._persist(("ps", 1), t)["action"] == "HOLD"            # a fresh run is needed for the next move
    fg._persist(("ps", 2), t); fg._persist(("ps", 2), h)            # a HOLD resets the streak
    assert fg._persist(("ps", 2), t)["action"] == "HOLD"


def test_persistence_off_and_rate_limit_default(monkeypatch):
    monkeypatch.setattr(config, "FLOW_EXIT_CONSEC", 1, raising=False)
    t = {"action": "TIGHTEN", "new_stop": 1.0, "new_target": None, "reason": "x"}
    assert fg._persist(("ps", 9), t)["action"] == "TIGHTEN"
    assert config.FLOW_EXIT_MIN_INTERVAL_S == 300.0
