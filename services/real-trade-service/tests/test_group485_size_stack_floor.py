"""E285 round 132: the stack of long-entry size penalties may not take the size under SIZE_STACK_FLOOR of the base risk (entry_engine/size_floor.py). Pure."""
from pathlib import Path

import config
from entry_engine import size_floor as sf

ROOT = Path(__file__).resolve().parents[1]


def _floor(monkeypatch, v):
    monkeypatch.setattr(config, "SIZE_STACK_FLOOR", v, raising=False)


def test_default_is_035():
    assert config.SIZE_STACK_FLOOR == 0.35


def test_stack_below_floor_is_lifted(monkeypatch):
    _floor(monkeypatch, 0.35)
    r, lifted = sf.floored_risk_pct(2.0, 0.2)
    assert lifted and abs(r - 0.7) < 1e-9


def test_stack_above_floor_is_untouched(monkeypatch):
    _floor(monkeypatch, 0.35)
    assert sf.floored_risk_pct(2.0, 1.0) == (1.0, False)


def test_never_above_base_and_ladder_raise_untouched(monkeypatch):
    _floor(monkeypatch, 0.35)
    assert sf.floored_risk_pct(2.0, 3.0) == (3.0, False)
    r, _ = sf.floored_risk_pct(2.0, 0.0001)
    assert r <= 2.0


def test_off_and_bad_input(monkeypatch):
    _floor(monkeypatch, 0.0)
    assert sf.floored_risk_pct(2.0, 0.1) == (0.1, False)
    _floor(monkeypatch, 0.35)
    assert sf.floored_risk_pct(None, 0.1) == (0.1, False)
    assert sf.floored_risk_pct(0.0, 0.1) == (0.1, False)
    assert sf.floored_risk_pct("x", "y") == ("y", False)


def test_opening_window_eased_only_when_stack_is_low(monkeypatch):
    _floor(monkeypatch, 0.35)
    assert sf.open_window_mult(2.0, 2.0, 0.5) == 0.5                       # stack 1.0 x 0.5 = 0.5 >= 0.35: window as is
    assert abs(sf.open_window_mult(2.0, 0.7, 0.5) - 1.0) < 1e-9            # stack already at the floor: window gives nothing more away
    assert abs(sf.open_window_mult(2.0, 1.0, 0.5) - 0.7) < 1e-9            # 0.5 x 0.5 = 0.25 -> eased to 0.7 so the whole stack is 0.35
    assert sf.open_window_mult(2.0, 1.0, 1.0) == 1.0                        # no window in force
    _floor(monkeypatch, 0.0)
    assert sf.open_window_mult(2.0, 0.1, 0.5) == 0.5


def test_whole_stack_never_below_floor(monkeypatch):
    _floor(monkeypatch, 0.35)
    base = 2.0
    for stack in (1.0, 0.8, 0.5, 0.3, 0.1, 0.02):
        for win in (1.0, 0.5):
            adj, _ = sf.floored_risk_pct(base, base * stack)
            m = sf.open_window_mult(base, adj, win)
            assert adj / base * m >= 0.35 - 1e-9 or win == 1.0 and adj / base >= 0.35 - 1e-9


def test_entry_wiring():
    s = (ROOT / "entry_engine" / "entry.py").read_text()
    assert "_risk_base_pct = adj_risk_pct" in s
    assert "size_floor.floored_risk_pct(_risk_base_pct, adj_risk_pct)" in s
    assert "size_floor.open_window_mult(_risk_base_pct, adj_risk_pct, opening_protect.size_mult(mode))" in s
    assert s.index("floored_risk_pct") < s.index("max_trade_risk = account_state.equity")
