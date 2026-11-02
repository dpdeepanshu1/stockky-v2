"""E285 round 126: the 3 % target cap no longer squeezes a noise-sized stop to 1.5:1 (RR_FLOOR), and a stop under 0.3 x daily ATR is widened instead of refused."""
import pytest

import config
from orders import adaptive, rr_floor
from screening import opening_gate


def _lv(target, stop, ltp=100.0):
    return adaptive.AdaptiveLevels(target_pct=target, stop_pct=stop, target_price=adaptive.round_to_tick(ltp * (1 + target / 100)),
                                   stop_price=adaptive.round_to_tick(ltp * (1 - stop / 100)), atr_proxy_pct=stop, basis="legacy", side="LONG")


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    for k, v in dict(SCALP_TARGET_CAP_PCT=3.0, SCALP_TARGET_CAP_MIN_RR=2.0, SCALP_TARGET_CAP_MIN_STOP_PCT=1.2, SCALP_TARGET_CAP_MIN_STOP_ATR_MULT=1.0,
                     SCALP_TARGET_CAP_KEEP_RR=True, SCALP_TARGET_CAP_MAX_PCT=5.0, ENTRY_MIN_REWARD_RISK=2.0, OPENING_GATE_WIDEN_STOP=True,
                     OPENING_GATE_MIN_STOP_ATR_FRAC=0.3, STOP_ATR_FLOOR_AFTER_WINDOW=True).items():
        monkeypatch.setattr(config, k, v, raising=False)


def test_logged_cases_now_keep_two_to_one():
    # 2 Nov log: "target cap 4.00% -> 3.00%, stop 2.00% -> 1.74%" then RR_FLOOR 1.7:1
    out = adaptive.apply_target_cap(_lv(4.0, 2.0), 100.0)
    assert out.target_pct == pytest.approx(4.0) and out.stop_pct == pytest.approx(2.0)
    assert rr_floor.reject_reason(out) is None


def test_cap_still_cuts_when_the_stop_does_not_need_room():
    out = adaptive.apply_target_cap(_lv(4.0, 1.0), 100.0)
    assert out.target_pct == pytest.approx(3.0) and out.stop_pct == pytest.approx(1.0)


def test_ceiling_holds_and_rr_floor_still_refuses_above_it():
    out = adaptive.apply_target_cap(_lv(7.0, 3.2), 100.0)       # 2 x 3.2 = 6.4 > 5.0 ceiling
    assert out.target_pct == pytest.approx(5.0)
    assert rr_floor.reject_reason(out) is not None


def test_keep_rr_off_restores_the_hard_cap(monkeypatch):
    monkeypatch.setattr(config, "SCALP_TARGET_CAP_KEEP_RR", False)
    out = adaptive.apply_target_cap(_lv(4.0, 2.0), 100.0)
    assert out.target_pct == pytest.approx(3.0) and rr_floor.reject_reason(out) is not None     # the logged 1.5:1 -> RR_FLOOR


class _PD:
    def __init__(self, atr): self.atr_pct = atr


def _atr(monkeypatch, atr):
    from screening import prev_day
    monkeypatch.setattr(prev_day, "peek", lambda s: _PD(atr) if atr is not None else None)
    monkeypatch.setattr(opening_gate, "is_active", lambda now=None: True)


def test_too_tight_stop_is_widened_with_the_target(monkeypatch):
    _atr(monkeypatch, 7.67)                                        # SPORTKING: stop 2.00 < 0.3 x 7.67 = 2.30
    out = adaptive.widen_to_atr_floor(_lv(4.0, 2.0), 100.0, "SPORTKING")
    assert out.stop_pct == pytest.approx(2.301, abs=1e-3) and out.target_pct == pytest.approx(4.602, abs=1e-3)
    assert opening_gate.stop_reject("SPORTKING", out.stop_pct) is None
    assert rr_floor.reject_reason(out) is None
    assert out.stop_price < 100.0 < out.target_price


def test_too_volatile_stock_stays_refused(monkeypatch):
    _atr(monkeypatch, 10.95)                                       # UTTAMSUGAR: floor 3.28 -> target 6.57 > 5.0
    lv = _lv(4.0, 1.67)
    assert adaptive.widen_to_atr_floor(lv, 100.0, "UTTAMSUGAR") is lv
    assert "STOP_TOO_TIGHT" in opening_gate.stop_reject("UTTAMSUGAR", lv.stop_pct)


def test_widen_is_off_unknown_atr_or_short(monkeypatch):
    _atr(monkeypatch, 7.67)
    lv = _lv(4.0, 2.0)
    monkeypatch.setattr(config, "OPENING_GATE_WIDEN_STOP", False)
    assert adaptive.widen_to_atr_floor(lv, 100.0, "X") is lv
    monkeypatch.setattr(config, "OPENING_GATE_WIDEN_STOP", True)
    _atr(monkeypatch, None)
    assert adaptive.widen_to_atr_floor(lv, 100.0, "X") is lv
    _atr(monkeypatch, 7.67)
    short = adaptive.AdaptiveLevels(target_pct=4.0, stop_pct=2.0, target_price=96.0, stop_price=102.0, side="SHORT")
    assert adaptive.widen_to_atr_floor(short, 100.0, "X") is short


def test_entry_calls_the_widening_before_the_gate():
    src = open(__import__("orders.entry", fromlist=["x"]).__file__).read()
    assert src.index("widen_to_atr_floor(levels") < src.index("opening_gate.stop_reject(candidate.symbol, levels.stop_pct)")
