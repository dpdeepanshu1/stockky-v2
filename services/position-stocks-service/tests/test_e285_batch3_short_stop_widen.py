"""E285 batch 3: a SHORT under the ATR stop floor is widened (target 2:1, size on the wider stop) instead of refused."""
import math

import config
from orders import adaptive, rr_floor
from screening import opening_gate, prev_day


class _PD:
    def __init__(self, atr): self.atr_pct = atr


def _setup(monkeypatch, atr=7.67):
    for k, v in dict(OPENING_GATE_WIDEN_STOP=True, SHORT_OPENING_GATE_WIDEN_STOP=True, OPENING_GATE_MIN_STOP_ATR_FRAC=0.3, STOP_ATR_FLOOR_AFTER_WINDOW=True,
                     SCALP_TARGET_CAP_MIN_RR=2.0, ENTRY_MIN_REWARD_RISK=2.0, SCALP_TARGET_CAP_MAX_PCT=5.0).items():
        monkeypatch.setattr(config, k, v, raising=False)
    monkeypatch.setattr(prev_day, "peek", lambda s: _PD(atr) if atr is not None else None)
    monkeypatch.setattr(opening_gate, "is_active", lambda now=None: True)


def _short(stop=1.5, target=3.0, ltp=100.0):
    return adaptive.AdaptiveLevels(target_pct=target, stop_pct=stop, target_price=adaptive.round_to_tick(ltp * (1 - target / 100)),
                                   stop_price=adaptive.round_to_tick(ltp * (1 + stop / 100)), side="SHORT")


def test_short_stop_widened_to_floor_target_follows_and_sides_are_right(monkeypatch):
    _setup(monkeypatch)                                    # floor = 0.3 x 7.67 = 2.30 %
    out = adaptive.widen_to_atr_floor(_short(), 100.0, "X")
    assert out.side == "SHORT" and out.stop_pct >= 2.301 - 1e-9 and out.target_pct >= 2.0 * 2.301 - 1e-3
    assert out.stop_price > 100.0 > out.target_price                      # stop above, target below the entry
    assert opening_gate.stop_reject("X", out.stop_pct) is None            # the gate no longer refuses it
    assert rr_floor.reject_reason(out) is None and rr_floor.geometry_reason(out, 100.0) is None


def test_stop_pct_is_rounded_up_never_under_the_floor(monkeypatch):
    _setup(monkeypatch, atr=7.6921)                        # floor 2.30763: a 4-dp round-down would be 2.3076 and be refused again
    out = adaptive.widen_to_atr_floor(_short(), 100.0, "X")
    assert out.stop_pct >= 0.3 * 7.6921


def test_short_switch_off_and_unknown_atr_leave_levels_alone(monkeypatch):
    _setup(monkeypatch)
    lv = _short()
    monkeypatch.setattr(config, "SHORT_OPENING_GATE_WIDEN_STOP", False)
    assert adaptive.widen_to_atr_floor(lv, 100.0, "X") is lv
    monkeypatch.setattr(config, "SHORT_OPENING_GATE_WIDEN_STOP", True)
    _setup(monkeypatch, atr=None)
    assert adaptive.widen_to_atr_floor(lv, 100.0, "X") is lv


def test_target_above_ceiling_is_still_refused(monkeypatch):
    _setup(monkeypatch, atr=9.0)                           # floor 2.7 x 2 = 5.4 > 5.0 ceiling: unchanged, the gate then refuses as before
    lv = _short()
    assert adaptive.widen_to_atr_floor(lv, 100.0, "X") is lv


def test_rupee_risk_is_unchanged_after_widening(monkeypatch):
    _setup(monkeypatch)
    budget_rs, ltp = 1000.0, 100.0
    lv = _short(stop=1.5)
    wide = adaptive.widen_to_atr_floor(lv, ltp, "X")
    budget_stop_pct = max(lv.stop_pct, wide.stop_pct)      # what plan_short does
    q_before = math.floor((budget_rs / (lv.stop_pct / 100.0)) / ltp)
    q_after = math.floor((budget_rs / (budget_stop_pct / 100.0)) / ltp)
    risk_before, risk_after = q_before * ltp * lv.stop_pct / 100.0, q_after * ltp * wide.stop_pct / 100.0
    assert q_after < q_before and risk_after <= budget_rs + 1e-6 and abs(risk_after - risk_before) <= ltp * wide.stop_pct / 100.0 * 1.0


def test_plan_short_wiring_order():
    src = open("orders/short_entry.py").read()
    assert src.index("widen_to_atr_floor(levels, live, sym)") < src.index("rr_floor.geometry_reason(levels, live)") < src.index("opening_gate.stop_reject(sym")
    assert src.index("budget_stop_pct = max(budget_stop_pct") < src.index("_short_position_value(db, budget_stop_pct")
    assert "SHORT_MIN_QTY" in src and src.index("SHORT_COST_GATE") < src.index("SHORT_MIN_QTY")
