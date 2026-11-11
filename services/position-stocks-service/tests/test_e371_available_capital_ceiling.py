"""E371: while positions are open, available_capital is never left above (pool total - open cost) by more than a tolerance; lowers only."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _mod():
    ns = {}
    exec(compile((ROOT / "capital" / "available_guard.py").read_text(encoding="utf-8"), "available_guard.py", "exec"), ns)
    return ns


def test_normal_flow_is_untouched():
    f = _mod()["clamped_available"]
    # pool 100k, 30k open, running figure 70k (and the bound is 70k): unchanged
    assert f(70000.0, 100000.0, 30000.0) == 70000.0
    assert f(40000.0, 100000.0, 30000.0) == 40000.0          # stuck LOW is never raised here


def test_drift_above_the_ceiling_is_lowered_to_it():
    f = _mod()["clamped_available"]
    assert f(95000.0, 100000.0, 30000.0) == 70000.0


def test_within_tolerance_is_left_alone():
    f = _mod()["clamped_available"]
    assert f(74000.0, 100000.0, 30000.0) == 74000.0          # 4k over, tolerance 5k
    assert f(76000.0, 100000.0, 30000.0) == 70000.0          # 6k over


def test_explicit_tolerance_and_bad_input():
    f = _mod()["clamped_available"]
    assert f(71000.0, 100000.0, 30000.0, 0.0) == 70000.0
    nan = float("nan")
    for args in ((nan, 100.0, 10.0), (50.0, nan, 10.0), (50.0, 100.0, nan), (50.0, 0.0, 10.0), (50.0, 100.0, 0.0), (50.0, 100.0, -5.0)):
        assert f(*args) == args[0] or args[0] != args[0]


def test_switch_is_read_each_call(monkeypatch):
    ns = _mod()
    monkeypatch.setenv("SCALP_AVAILABLE_CEILING_CLAMP", "0")
    assert ns["enabled"]() is False
    monkeypatch.setenv("SCALP_AVAILABLE_CEILING_CLAMP", "1")
    assert ns["enabled"]() is True
    monkeypatch.delenv("SCALP_AVAILABLE_CEILING_CLAMP")
    assert ns["enabled"]() is True


def test_sync_wiring_uses_the_full_cost_before_the_margin_fraction():
    s = (ROOT / "capital" / "ledger.py").read_text(encoding="utf-8")
    assert s.index("_committed_full = float(own_committed_capital") < s.index("own_committed_capital = own_committed_capital * _margin_frac")
    assert "available_guard.clamped_available(row.available_capital, row.total_allocated_capital, _committed_full)" in s
    # the flat-pool reset and the zero-drain reset stay ahead of / after it unchanged
    assert s.index("if open_count == 0:") < s.index("elif available_guard.enabled()") < s.index("elif row.available_capital <= 0:")
