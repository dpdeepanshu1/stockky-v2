"""E360: a position whose stop is missing / NaN / infinite / <= 0 no longer counts as zero heat."""
import math
import os
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from risk_engine import portfolio_heat as ph
from execution import heat_state as hs

BAD = [None, float("nan"), float("inf"), float("-inf"), 0, -5, "x"]


def test_old_behaviour_kept_when_no_fallback_given():
    assert all(ph.stop_risk(100, b, 10) == 0.0 for b in BAD)
    assert ph.stop_risk(100, 98, 10) == 20.0 and ph.stop_risk(100, 101, 10) == 0.0


def test_bad_stop_uses_initial_stop_distance_first():
    for b in BAD:
        assert ph.stop_risk(100, b, 10, "LONG", stop_distance=3.0, max_stop_pct=6.0) == 30.0


def test_bad_stop_without_distance_uses_max_stop_pct():
    for b in BAD:
        for d in (None, float("nan"), 0, -1):
            assert math.isclose(ph.stop_risk(100, b, 10, "LONG", stop_distance=d, max_stop_pct=6.0), 60.0)


def test_good_stop_ignores_the_fallbacks():
    assert ph.stop_risk(100, 98, 10, "LONG", stop_distance=9.0, max_stop_pct=6.0) == 20.0
    assert ph.stop_risk(100, 101, 10, "LONG", stop_distance=9.0, max_stop_pct=6.0) == 0.0   # breakeven stop still risks nothing
    assert ph.stop_risk(100, 102, 10, "SHORT", stop_distance=9.0, max_stop_pct=6.0) == 20.0


def test_short_with_bad_stop_uses_the_same_fallback():
    assert math.isclose(ph.stop_risk(100, None, 10, "SHORT", max_stop_pct=5.0), 50.0)


def test_position_heat_passes_the_fallbacks_through():
    assert ph.position_heat(100, None, 10, "LONG", True, 1.5, stop_distance=2.0) == (20.0, 15.0)


def _pos(stop, dist=None, sym="ZZZ"):
    return SimpleNamespace(symbol=sym, avg_entry_price=100.0, current_stop=stop, qty_open=10, initial_stop_distance=dist,
                           opened_at=None, entry_product_type="CNC", broker_imported=False)


def test_heat_state_counts_a_nan_stop(monkeypatch):
    monkeypatch.setattr(config, "HEAT_MISSING_STOP_PCT", 6.0, raising=False)
    monkeypatch.setattr(hs, "_carried", lambda p: False)
    assert hs.positions_heat([_pos(float("nan"))]) == (60.0, 0.0)
    assert hs.positions_heat([_pos(None, dist=2.5)]) == (25.0, 0.0)
    assert hs._heat_strict([_pos(float("nan"))]) == (60.0, 0.0)
    assert hs.positions_heat([_pos(97.0)]) == (30.0, 0.0)


def test_heat_state_zero_setting_is_the_old_behaviour(monkeypatch):
    monkeypatch.setattr(config, "HEAT_MISSING_STOP_PCT", 0.0, raising=False)
    monkeypatch.setattr(hs, "_carried", lambda p: False)
    assert hs.positions_heat([_pos(float("nan"))]) == (0.0, 0.0)


def test_sector_risk_counts_a_bad_stop(monkeypatch):
    from risk_engine import sector_map
    monkeypatch.setattr(config, "HEAT_MISSING_STOP_PCT", 6.0, raising=False)
    monkeypatch.setattr(sector_map, "sector_of", lambda s: "Banks")
    assert hs.sector_risk([_pos(None)]) == {"Banks:LONG": 60.0}
    assert hs._sector_strict([_pos(float("nan"), dist=2.0)]) == {"Banks:LONG": 20.0}


def test_default_setting_exists():
    assert getattr(config, "HEAT_MISSING_STOP_PCT", None) == 6.0


def test_position_stocks_callers_pass_max_stop_pct():
    root = pathlib.Path(__file__).resolve().parents[2] / "position-stocks-service" / "capital"
    led = (root / "ledger.py").read_text(encoding="utf-8")
    gate = (root / "heat_gate.py").read_text(encoding="utf-8")
    assert led.count("max_stop_pct=float(config.MAX_STOP_PCT)") == 2
    assert "max_stop_pct=" in gate
