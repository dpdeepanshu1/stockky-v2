"""E285 round 129: SOFT_PENALTIES_TOO_HIGH skips at SOFT_PENALTY_SKIP_AT (6.0), not at the old total of 4 where the size was already at its floor;
a live price already above the cached resistance is a size penalty, not a 'no room' refusal."""
from pathlib import Path

import pytest

import config
from orders import setup_engine, soft_gates

ENTRY_SRC = (Path(__file__).resolve().parents[1] / "orders" / "entry.py").read_text()


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    for k, v in dict(SOFT_GATES_MODE="penalty", SOFT_GATE_MAX_PENALTY=3.0, SOFT_PENALTY_SKIP_AT=6.0, STRUCTURE_ROOM_BEYOND_PENALTY=1.5,
                     STRUCTURE_MAX_CHASE_ATR=4.0, STRUCTURE_MAX_STRETCH_ATR=5.0, STRUCTURE_MIN_ROOM_ATR=0.3, STRUCTURE_HARD_MULT=1.75).items():
        monkeypatch.setattr(config, k, v, raising=False)


# ---- the skip total -------------------------------------------------------------------------------------------------------------
def test_skip_limit_default_and_never_under_four(monkeypatch):
    assert soft_gates.skip_limit() == 6.0
    monkeypatch.setattr(config, "SOFT_PENALTY_SKIP_AT", 2.0)
    assert soft_gates.skip_limit() == 4.0
    monkeypatch.setattr(config, "SOFT_PENALTY_SKIP_AT", 4.0)
    assert soft_gates.skip_limit() == 4.0


def test_total_between_four_and_six_is_no_longer_a_refusal():
    pens = {"vwap": 2.0, "preopen_gap": 2.5}                      # total 4.5: the old rule skipped it
    assert soft_gates.total_skip_reason(pens, 4.0) is not None
    assert soft_gates.total_skip_reason(pens, soft_gates.skip_limit()) is None
    assert soft_gates.total_skip_reason({"a": 3.0, "b": 3.0}, soft_gates.skip_limit()).startswith("SOFT_PENALTIES_TOO_HIGH")   # two gates at the cap still skip


def test_size_of_such_an_entry_is_the_floor():
    assert setup_engine.SIZE_FLOOR == 0.25
    assert soft_gates.late_size(None, {}, {"vwap": 2.0, "preopen_gap": 2.5}, setup_engine.SIZE_FLOOR) == 0.25


# ---- the setup engine --------------------------------------------------------------------------------------------------------------
def _facts(**kw):
    from datetime import datetime
    f = dict(side="LONG", service="PS", now=datetime(2026, 11, 2, 9, 40), allowed={setup_engine.S1}, event_polarity=1, event_priced_in=0.2,
             price=101.0, preopen_price=100.0, or_high=100.5, bar_close=101.0, bar_closed_after_range=True, above_vwap=True, tape=1,
             sector_third="top", penalties={"vwap": 2.0, "preopen_gap": 2.5})
    f.update(kw)
    return f


def test_engine_skips_at_four_without_the_new_fact_and_trades_with_it():
    base = setup_engine.classify(_facts())
    assert base.get("skip") == "PENALTY_TOO_HIGH"                              # unchanged default: Real Trade and the backtest script
    res = setup_engine.classify(_facts(penalty_skip=6.0))
    assert res.get("id") == setup_engine.S1 and res["size_mult"] == 0.25
    assert setup_engine.classify(_facts(penalty_skip=6.0, penalties={"a": 3.0, "b": 3.0})).get("skip") == "PENALTY_TOO_HIGH"


def test_the_fact_can_only_raise_the_limit():
    assert setup_engine.classify(_facts(penalty_skip=2.0)).get("skip") == "PENALTY_TOO_HIGH"
    assert setup_engine.classify(_facts(penalty_skip=True)).get("skip") == "PENALTY_TOO_HIGH"
    assert setup_engine.classify(_facts(penalty_skip=float("nan"))).get("skip") == "PENALTY_TOO_HIGH"


def test_both_setup_engine_copies_stay_identical():
    here = Path(__file__).resolve().parents[1]
    rt = here.parent / "real-trade-service" / "entry_engine" / "setup_engine.py"
    assert (here / "orders" / "setup_engine.py").read_bytes() == rt.read_bytes()


def test_entry_uses_the_new_limit_everywhere():
    assert "soft_gates.total_skip_reason(_penalties, setup_engine.PENALTY_SKIP)" not in ENTRY_SRC
    assert ENTRY_SRC.count("soft_gates.total_skip_reason(_penalties, soft_gates.skip_limit())") == 4
    assert "penalty_skip=soft_gates.skip_limit()" in ENTRY_SRC


# ---- structure: room -------------------------------------------------------------------------------------------------------------
def _ver(room, extra=None):
    reasons = [f"only {max(0.0, room):.1f} daily ATR of room to the next resistance (min 0.3)"] + list(extra or [])
    return {"would_reject": True, "reasons": reasons, "reason": "; ".join(reasons), "metrics": {"chase_atr": 1.0, "stretch_atr": 1.0, "room_atr": room}}


def test_price_beyond_resistance_is_a_penalty_not_a_refusal():
    why, pens = soft_gates.structure_penalties(_ver(-0.8))
    assert why is None and pens == {"structure_room": 1.5}


def test_beyond_resistance_with_the_switch_off_is_the_old_refusal(monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_ROOM_BEYOND_PENALTY", 0.0)
    why, pens = soft_gates.structure_penalties(_ver(-0.8))
    assert why and not pens


def test_still_under_the_resistance_with_almost_no_room_is_refused():
    why, pens = soft_gates.structure_penalties(_ver(0.05))                     # under a quarter of the floor, not beyond: unchanged
    assert why and not pens


def test_normal_room_shortfall_is_still_scaled():
    why, pens = soft_gates.structure_penalties(_ver(0.2))
    assert why is None and 1.0 < pens["structure_room"] < 3.0


def test_penalty_is_capped_by_the_per_gate_cap(monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_ROOM_BEYOND_PENALTY", 3.0)
    monkeypatch.setattr(config, "SOFT_GATE_MAX_PENALTY", 2.0)
    assert soft_gates.structure_penalties(_ver(-0.5))[1] == {"structure_room": 2.0}


def test_thin_volume_or_bearish_reasons_still_refuse_a_beyond_resistance_breakout():
    why, pens = soft_gates.structure_penalties(_ver(-0.8, extra=["breakout 2 bar(s) ago came on thin volume (0.80x the 20-day average, wants 1.2x)"]))
    assert why and not pens
    why, pens = soft_gates.structure_penalties(_ver(-0.8, extra=["daily structure is bearish"]))
    assert why and not pens


def test_veto_mode_keeps_the_gate_refusal(monkeypatch):
    monkeypatch.setattr(config, "SOFT_GATES_MODE", "veto")
    why, pens = soft_gates.structure_penalties(_ver(-0.8))
    assert why and not pens
