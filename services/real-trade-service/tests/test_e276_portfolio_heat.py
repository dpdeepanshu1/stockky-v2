"""E276 round 50: Real Trade's portfolio-heat cap counts the peer's open risk and a gap allowance on carried holds, against the combined pool.
Pure maths (risk_engine/portfolio_heat.py), the risk engine's check 6 and execution/heat_state.py, with the DB stubbed.
Plain runner: python3 tests/test_e276_portfolio_heat.py (pytest also collects it)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk_engine import portfolio_heat as ph


def test_stop_risk_long_and_short_and_breakeven():
    assert ph.stop_risk(100, 98, 10) == 20
    assert ph.stop_risk(100, 102, 10, "SHORT") == 20
    assert ph.stop_risk(100, 100, 10) == 0 and ph.stop_risk(100, 103, 10) == 0   # stop at / past breakeven risks nothing
    assert ph.stop_risk("x", None, 5) == 0                                         # bad numbers count as 0, never raise


def test_gap_allowance_only_when_carried():
    assert ph.position_heat(100, 98, 10, "LONG", False, 1.5) == (20, 0.0)
    r, g = ph.position_heat(100, 98, 10, "LONG", True, 1.5)
    assert r == 20 and abs(g - 15.0) < 1e-9


def test_p95_gap_needs_enough_days():
    opens = [100.0] * 30
    closes = [100.0] * 30
    assert ph.p95_gap_pct(opens, closes, 20) == 0.0
    assert ph.p95_gap_pct(opens[:10], closes[:10], 20) is None
    opens2 = [100.0] + [99.0] * 29                                                  # one 1 % gap down, then flat opens vs 100 closes
    assert ph.p95_gap_pct(opens2, closes, 20) == 1.0


def test_check_combines_peer_and_pool():
    ok, after, cap = ph.check(100, 0, 100, 100, 7000, 7000, 5.0)                   # pool 14000, cap 700, total 300
    assert ok and abs(cap - 700) < 1e-9 and abs(after - 300 / 14000 * 100) < 1e-9
    ok, _, _ = ph.check(300, 0, 300, 200, 7000, 7000, 5.0)                          # 800 > 700
    assert not ok
    ok, _, cap = ph.check(300, 0, 0, 100, 7000, 0, 5.0)                             # no peer row: own pool alone (old cap 350)
    assert not ok and abs(cap - 350) < 1e-9
    ok, _, _ = ph.check(9999, 9999, 9999, 9999, 7000, 7000, 0)                      # limit 0 = no limit
    assert ok


def _account(**kw):
    from risk_engine.engine import AccountState
    base = dict(equity=10000.0, risk_per_trade_pct=5.0, max_daily_loss_pct=50.0, max_concurrent_positions=5, max_portfolio_risk_pct=5.0, stale_data_seconds=30,
                max_tick_volatility_mult=2.0, allow_pyramiding=False, realized_pnl_today=0.0, open_position_count=0, open_position_symbols=set(),
                open_positions_total_risk=0.0, trading_globally_paused=False, market_is_open=True, cash_available=10000.0)
    base.update(kw)
    return AccountState(**base)


def _intent(qty=10, entry=100.0, stop=98.0):
    from risk_engine.engine import OrderIntent
    return OrderIntent(mode="DEMO", symbol="ABC", side="BUY", qty=qty, entry_price=entry, stop_price=stop)


def test_engine_boundary_old_cap():
    from risk_engine.engine import evaluate
    ok = evaluate(_intent(), _account(open_positions_total_risk=470.0))                   # 470 + 20 = 490 <= 500
    bad = evaluate(_intent(), _account(open_positions_total_risk=490.0))                  # 510 > 500
    assert ok.check_name != "max_portfolio_risk", ok
    assert bad.check_name == "max_portfolio_risk", bad


def test_engine_peer_risk_and_gap_tighten_the_cap():
    from risk_engine.engine import evaluate
    # own pool 10000 + peer pool 10000 -> cap 1000; own 400 + gap 150 + peer 400 + new 20 = 970 -> ok
    ok = evaluate(_intent(), _account(open_positions_total_risk=400.0, open_positions_gap_allowance=150.0, other_service_open_risk=400.0, other_service_capital_base=10000.0))
    assert ok.check_name != "max_portfolio_risk", ok
    # peer 450 -> 1020 > 1000
    bad = evaluate(_intent(), _account(open_positions_total_risk=400.0, open_positions_gap_allowance=150.0, other_service_open_risk=450.0, other_service_capital_base=10000.0))
    assert bad.check_name == "max_portfolio_risk" and "position-stocks-service" in bad.reason, bad


def test_engine_gap_allowance_alone_can_reject():
    from risk_engine.engine import evaluate
    bad = evaluate(_intent(), _account(open_positions_total_risk=400.0, open_positions_gap_allowance=100.0))   # 520 > 500
    assert bad.check_name == "max_portfolio_risk", bad


import types as _types

_KEYS = ("sqlalchemy", "sqlalchemy.orm", "models", "execution.shared_heat", "execution.heat_state")


def _load_heat_state():
    """import execution.heat_state with sqlalchemy / models stubbed when they are not installed (the sandbox has neither); returns (module, restore)."""
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


class _Pos(SimpleNamespace):
    pass


def test_heat_state_positions_heat_carried_vs_intraday():
    heat_state, restore = _load_heat_state()
    try:
        _carried_body(heat_state)
    finally:
        restore()


def _carried_body(heat_state):
    today = datetime.now(timezone.utc)
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    p_new = _Pos(avg_entry_price=100.0, current_stop=98.0, qty_open=10, opened_at=today, entry_decision_label=None)
    p_old = _Pos(avg_entry_price=100.0, current_stop=98.0, qty_open=10, opened_at=old, entry_decision_label=None)
    p_swing = _Pos(avg_entry_price=100.0, current_stop=101.0, qty_open=10, opened_at=today, entry_decision_label="SWING_MOMENTUM")
    r, g = heat_state.positions_heat([p_new])
    assert r == 20 and g == 0.0                                                        # opened today, not a swing: no gap allowance
    r, g = heat_state.positions_heat([p_old])
    assert r == 20 and abs(g - 15.0) < 1e-9                                            # carried from an earlier day
    r, g = heat_state.positions_heat([p_swing])
    assert r == 0 and abs(g - 15.0) < 1e-9                                             # swing hold, stop above entry: no stop risk, allowance still counts


def test_heat_state_demo_and_switch_off_are_zero():
    heat_state, restore = _load_heat_state()
    try:
        _off_body(heat_state)
    finally:
        restore()


def _off_body(heat_state):
    import config
    assert heat_state.own_heat(None, "DEMO") == (0.0, 0.0) and heat_state.peer_heat(None, "DEMO") == (0.0, 0.0)
    config.PORTFOLIO_HEAT_SHARED_ENABLED = False
    try:
        assert heat_state.own_heat(None, "REAL", [_Pos(avg_entry_price=100.0, current_stop=90.0, qty_open=10, opened_at=None)]) == (0.0, 0.0)
        assert heat_state.peer_heat(None, "REAL") == (0.0, 0.0)
    finally:
        config.PORTFOLIO_HEAT_SHARED_ENABLED = True


def test_gap_cache_p95_feeds_the_allowance():
    heat_state, restore = _load_heat_state()
    try:
        from execution import gap_cache
        import config
        gap_cache.reset()
        pos = _Pos(symbol="ABC", avg_entry_price=100.0, current_stop=98.0, qty_open=10, opened_at=datetime(2020, 1, 1, tzinfo=timezone.utc), entry_decision_label=None)
        assert abs(heat_state.gap_pct_for("ABC") - 1.5) < 1e-9                         # not cached yet: the flat default
        bars = [(i, 100.0, 101.0, 99.0, 100.0, 1.0) for i in range(40)]
        bars[10] = (10, 97.0, 101.0, 96.0, 100.0, 1.0)                                   # one 3 % gap down among 39 pairs
        bars[11] = (11, 100.0, 101.0, 99.0, 100.0, 1.0)
        assert gap_cache.warm(["ABC"], fetch=lambda s: bars, sync=True)
        v = gap_cache.peek("ABC")
        assert v is not None and 0.0 <= v <= 3.0
        assert abs(heat_state.gap_pct_for("abc.ns") - max(0.5, v)) < 1e-9
        gap_cache.warm(["BAD"], fetch=lambda s: (_ for _ in ()).throw(RuntimeError("x")), sync=True)   # failure -> None -> default
        assert gap_cache.peek("BAD") is None and abs(heat_state.gap_pct_for("BAD") - 1.5) < 1e-9
        assert gap_cache.warm(["ABC"], fetch=lambda s: bars, sync=True) is False         # cached: nothing to do
        config.HEAT_GAP_P95_ENABLED = False
        try:
            assert abs(heat_state.gap_pct_for("ABC") - 1.5) < 1e-9
        finally:
            config.HEAT_GAP_P95_ENABLED = True
        gap_cache.reset()
    finally:
        restore()


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); n += 1
    print(f"{n} passed")
