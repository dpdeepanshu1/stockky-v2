"""E276 round 60: the same-sector rule - a new entry in a sector that already holds an open position counts 1.5x (its own risk plus that sector's open risk).
Pure maths (risk_engine/portfolio_heat.py), the sector map, the risk engine's check 6 and execution/heat_state.py sector split. Plain runner: python3 tests/test_e276_sector_heat.py"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk_engine import portfolio_heat as ph
from risk_engine import sector_map


def test_surcharge_maths():
    assert ph.sector_surcharge(0, 100, 1.5) == 0.0                 # nothing open there yet: no surcharge
    assert abs(ph.sector_surcharge(100, 100, 1.5) - 100.0) < 1e-9  # two 100s count 300 instead of 200
    assert ph.sector_surcharge(100, 100, 1.0) == 0.0 and ph.sector_surcharge(100, 0, 1.5) == 0.0
    assert ph.sector_surcharge("x", None, "y") == 0.0              # bad numbers never raise


def test_sector_key_and_merge():
    assert ph.sector_key("Banks", "BUY") == "Banks:LONG" and ph.sector_key("Banks", "SELL") == "Banks:SHORT"
    m = ph.merge_sector_risk({"Banks:LONG": 50}, {"Banks:LONG": 25, "IT:LONG": 10}, None, {"bad": "x"})
    assert m == {"Banks:LONG": 75.0, "IT:LONG": 10.0, "bad": 0.0}


def test_check_adds_the_surcharge():
    ok, after, cap = ph.check(100, 0, 0, 100, 10000, 0, 2.5, 0.0)       # 200 / 10000 = 2 % <= 2.5 %
    assert ok and abs(after - 2.0) < 1e-9
    ok, after, cap = ph.check(100, 0, 0, 100, 10000, 0, 2.5, 100.0)     # 300 / 10000 = 3 % > 2.5 %
    assert not ok and abs(after - 3.0) < 1e-9
    assert ph.check(100, 0, 0, 100, 10000, 0, 2.5) == ph.check(100, 0, 0, 100, 10000, 0, 2.5, 0.0)   # default = the old maths


def test_sector_map_known_and_other():
    assert sector_map.sector_of("HDFCBANK") == "Banks" and sector_map.sector_of("SBIN") == "Banks"
    assert sector_map.sector_of("NOSUCHSTOCK") == "Other"


import types as _types

_KEYS = ("sqlalchemy", "sqlalchemy.orm", "models", "execution.shared_heat", "execution.heat_state")


def _load_heat_state():
    """execution.heat_state with sqlalchemy / models stubbed when not installed (same approach as test_e276_portfolio_heat.py); returns (module, restore)."""
    orig = {k: sys.modules.get(k) for k in _KEYS}
    try:
        import sqlalchemy.orm  # noqa: F401
        import models  # noqa: F401
    except Exception:  # noqa: BLE001
        sa = _types.ModuleType("sqlalchemy"); orm = _types.ModuleType("sqlalchemy.orm"); orm.Session = object; sa.orm = orm
        m = _types.ModuleType("models"); m.SharedHeat = object; m.SharedHeatSectors = object
        sys.modules.update({"sqlalchemy": sa, "sqlalchemy.orm": orm, "models": m})
    for k in ("execution.shared_heat", "execution.heat_state"):
        sys.modules.pop(k, None)
    from execution import heat_state

    def restore():
        for k, v in orig.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return heat_state, restore


def test_heat_state_sector_split_skips_other_and_riskless():
    heat_state, restore = _load_heat_state()
    try:
        pos = [SimpleNamespace(symbol="HDFCBANK", avg_entry_price=100, current_stop=98, qty_open=10),   # 20
               SimpleNamespace(symbol="SBIN", avg_entry_price=100, current_stop=99, qty_open=10),       # 10, same sector
               SimpleNamespace(symbol="TCS", avg_entry_price=100, current_stop=101, qty_open=10),       # stop above entry: 0
               SimpleNamespace(symbol="NOSUCHSTOCK", avg_entry_price=100, current_stop=90, qty_open=10)]
        assert heat_state.sector_risk(pos) == {"Banks:LONG": 30.0}
        assert heat_state.own_sector_risk(None, "DEMO") == {} and heat_state.peer_sector_risk(None, "DEMO") == {}   # DEMO never counts
    finally:
        restore()


def _account(**kw):
    from risk_engine.engine import AccountState
    base = dict(equity=10000.0, risk_per_trade_pct=5.0, max_daily_loss_pct=50.0, max_concurrent_positions=5, max_portfolio_risk_pct=5.0, stale_data_seconds=30,
                max_tick_volatility_mult=2.0, allow_pyramiding=False, realized_pnl_today=0.0, open_position_count=0, open_position_symbols=set(),
                open_positions_total_risk=0.0, trading_globally_paused=False, market_is_open=True, cash_available=10000.0)
    base.update(kw)
    return AccountState(**base)


def _intent(symbol, qty=10, entry=100.0, stop=98.0):
    from risk_engine.engine import OrderIntent
    return OrderIntent(mode="DEMO", symbol=symbol, side="BUY", qty=qty, entry_price=entry, stop_price=stop)


def test_engine_same_sector_is_refused_where_a_different_sector_fits():
    from risk_engine.engine import evaluate
    # cap 500, 470 already open (all Banks), order risk 20: 490 fits a plain check, but a second bank adds 0.5 x (470 + 20) = 245 -> refused.
    held = dict(open_positions_total_risk=470.0, open_positions_sector_risk={"Banks:LONG": 470.0})
    other = evaluate(_intent("TCS"), _account(**held))
    same = evaluate(_intent("SBIN"), _account(**held))
    assert other.check_name != "max_portfolio_risk"
    assert same.check_name == "max_portfolio_risk" and "same-sector" in same.reason


def test_engine_peer_sector_counts_too_and_unlisted_names_do_not():
    from risk_engine.engine import evaluate
    held = dict(open_positions_total_risk=0.0, other_service_open_risk=470.0, other_service_capital_base=0.0, other_service_sector_risk={"Banks:LONG": 470.0})
    assert evaluate(_intent("SBIN"), _account(**held)).check_name == "max_portfolio_risk"
    assert evaluate(_intent("NOSUCHSTOCK"), _account(**held)).check_name != "max_portfolio_risk"


def test_engine_without_sector_data_is_the_old_maths():
    from risk_engine.engine import evaluate
    assert evaluate(_intent("SBIN"), _account(open_positions_total_risk=470.0)).check_name != "max_portfolio_risk"


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1
    print(f"OK - {n} tests")
