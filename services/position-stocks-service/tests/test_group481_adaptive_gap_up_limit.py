"""E285 round 128: OPENING_GATE:GAP_UP follows the stock's own ATR (the fixed +3 % refused every wide-ranging stock: 48 skips in 2 h on 2 Nov)."""
import pytest

import config
from screening import opening_gate, preopen, prev_day


class _PD:
    def __init__(self, atr): self.atr_pct = atr


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    for k, v in dict(OPENING_GATE_MAX_GAP_UP_PCT=3.0, OPENING_GATE_GAP_ADAPTIVE=True, OPENING_GATE_GAP_UP_HARD_PCT=8.0, PREOPEN_GAP_ATR_MULT=1.5,
                     PREOPEN_GAP_ATR_FLOOR_PCT=1.5).items():
        monkeypatch.setattr(config, k, v, raising=False)


def _atr(monkeypatch, atr):
    monkeypatch.setattr(prev_day, "get", lambda s: _PD(atr) if atr is not None else None)


def test_wide_ranging_stock_gets_its_own_limit(monkeypatch):
    _atr(monkeypatch, 6.4)                       # QPOWER-like: 1.5 x 6.4 = 9.6 -> capped by the 8.0 ceiling
    assert opening_gate.gap_up_limit("X") == pytest.approx(8.0)
    _atr(monkeypatch, 4.0)                       # 1.5 x 4.0 = 6.0
    assert opening_gate.gap_up_limit("X") == pytest.approx(6.0)


def test_calm_stock_keeps_the_fixed_limit(monkeypatch):
    _atr(monkeypatch, 1.5)                       # RELIANCE-like: 2.25 < 3.0
    assert opening_gate.gap_up_limit("RELIANCE") == pytest.approx(3.0)


def test_unknown_atr_or_switch_off_is_the_old_fixed_limit(monkeypatch):
    _atr(monkeypatch, None)
    assert opening_gate.gap_up_limit("X") == pytest.approx(3.0)
    _atr(monkeypatch, 6.0)
    monkeypatch.setattr(config, "OPENING_GATE_GAP_ADAPTIVE", False)
    assert opening_gate.gap_up_limit("X") == pytest.approx(3.0)


def test_hard_ceiling_zero_means_no_ceiling_and_gate_off_stays_off(monkeypatch):
    _atr(monkeypatch, 10.0)
    monkeypatch.setattr(config, "OPENING_GATE_GAP_UP_HARD_PCT", 0.0)
    assert opening_gate.gap_up_limit("X") == pytest.approx(15.0)
    monkeypatch.setattr(config, "OPENING_GATE_MAX_GAP_UP_PCT", 0.0)
    assert opening_gate.gap_up_limit("X") == 0.0


def test_a_ceiling_below_the_fixed_limit_never_lowers_it(monkeypatch):
    _atr(monkeypatch, 6.0)
    monkeypatch.setattr(config, "OPENING_GATE_GAP_UP_HARD_PCT", 2.0)
    assert opening_gate.gap_up_limit("X") == pytest.approx(3.0)


def test_reject_reason_uses_the_limit():
    import inspect
    src = inspect.getsource(opening_gate.reject_reason)
    assert "gap_up_limit(symbol)" in src and "OPENING_GATE:GAP_UP:{gap:+.2f}% > +{_gap_up_lim:.2f}%" in src
