"""Round 252: numbers that cannot work (a stop of 150 %, a trail of 0, a risk of 0 or over 100 %, ...) refuse an entry with a reason instead of building a position with a stop that can never fire."""
from __future__ import annotations

from datetime import date

import pytest

from fno import gates, paper
from fno.models import ContractRef
from fno.params import Params
from test_engine_paper import NOW, engine, make_cfg
from test_round248 import db  # noqa: F401  (the shared fixture)


def test_default_params_have_no_problems():
    assert gates.param_problems(Params()) == []


@pytest.mark.parametrize("kw,needle", [
    ({"sl_pct": 0.0}, "FNO_SL_PCT"), ({"sl_pct": 100.0}, "FNO_SL_PCT"), ({"sl_pct": 150.0}, "FNO_SL_PCT"), ({"sl_pct": float("nan")}, "FNO_SL_PCT"),
    ({"target_pct": 0.0}, "FNO_TARGET_PCT"), ({"trail_arm_pct": -1.0}, "FNO_TRAIL_ARM_PCT"),
    ({"trail_pct": 0.0}, "FNO_TRAIL_PCT"), ({"trail_pct": 100.0}, "FNO_TRAIL_PCT"),
    ({"risk_per_trade_pct": 0.0}, "FNO_RISK_PER_TRADE_PCT"), ({"risk_per_trade_pct": 101.0}, "FNO_RISK_PER_TRADE_PCT"),
    ({"max_daily_loss_pct": 0.0}, "FNO_MAX_DAILY_LOSS_PCT"), ({"max_daily_loss_pct": 500.0}, "FNO_MAX_DAILY_LOSS_PCT"),
    ({"slippage_pct": -0.5}, "FNO_SLIPPAGE_PCT"), ({"slippage_pct": 60.0}, "FNO_SLIPPAGE_PCT"), ({"max_lots": 0}, "FNO_MAX_LOTS"),
])
def test_each_unusable_number_is_named(kw, needle):
    why = gates.param_problems(Params().with_(**kw))
    assert len(why) == 1 and needle in why[0], why


def test_the_tightest_and_loosest_calibration_values_are_accepted():
    # the Calibration page keeps every switch inside these ranges (control/catalog.py): none of them may be refused by the hard limits
    for kw in ({"sl_pct": 5.0}, {"sl_pct": 60.0}, {"trail_pct": 5.0}, {"trail_pct": 60.0}, {"risk_per_trade_pct": 0.1}, {"risk_per_trade_pct": 3.0},
               {"max_daily_loss_pct": 0.5}, {"max_daily_loss_pct": 20.0}, {"slippage_pct": 0.0}, {"slippage_pct": 5.0}, {"target_pct": 10.0}, {"target_pct": 300.0}, {"trail_arm_pct": 5.0}, {"trail_arm_pct": 200.0}):
        assert gates.param_problems(Params().with_(**kw)) == [], kw


def test_entry_gates_refuse_with_the_reason():
    from fno.gates import DayState
    ok, why = gates.entry_gates(NOW, date(2026, 11, 17), 14.0, 1.0, DayState(), Params().with_(sl_pct=150.0), key="NIFTY:CE")
    assert not ok and any("FNO_SL_PCT" in w for w in why)
    ok2, why2 = gates.entry_gates(NOW, date(2026, 11, 17), 14.0, 1.0, DayState(), Params(), key="NIFTY:CE")
    assert ok2 and why2 == []


def test_engine_opens_nothing_with_a_stop_that_could_never_fire(db):
    e = engine(db, cfg=make_cfg(FNO_UNDERLYINGS="NIFTY", FNO_SL_PCT=150.0))
    r = e.tick(NOW)
    assert r["entries"] == [] and db.open_positions() == []
    assert any(ev["kind"] == "skip" and "FNO_SL_PCT" in str(ev) for ev in db.events())


def test_why_it_matters_a_150_percent_stop_has_no_price_that_fires():
    c = ContractRef("NIFTY", "2026-11-17", 24500.0, "CE", 65, 111, "NSE_FNO", 0.05)
    pp = Params().with_(sl_pct=150.0)
    pos = paper.open_position(c, 1, 100.0, NOW.timestamp(), pp)
    assert pos.sl_price <= 0                                              # a stop below zero
    assert paper.evaluate_tick(pos, 0.05, pp) is None                     # even a premium of one tick does not stop it out
