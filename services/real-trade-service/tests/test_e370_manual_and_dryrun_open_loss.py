"""E370: manual BUYs and the /risk-engine/check dry run feed the open positions' unrealized P&L (E368) into the account state."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _src(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_manual_account_state_takes_and_sets_open_pnl():
    s = _src("manual_engine.py")
    assert re.search(r"def _account_state\(db: Session, mode: str, gate_armed: bool, open_pnl: float = 0\.0\)", s)
    assert "open_unrealized_pnl=float(open_pnl or 0.0)" in s


def test_manual_buy_path_computes_it_once_with_the_patchable_quote_fn():
    s = _src("manual_engine.py")
    assert s.count("open_unrealized_pnl(db, mode, quotes_fn=get_quotes)") == 1
    assert "_account_state(db, mode, gate_armed, open_pnl=_open_pnl)" in s
    # it sits inside the BUY branch, before the SELL branch: a SELL never reaches the risk engine or this call
    assert s.index("quotes_fn=get_quotes)") > s.index('if side == "BUY":')


def test_risk_check_dry_run_uses_it_for_buys_only():
    s = _src("main.py")
    assert 'if body.side.upper() == "BUY":   # E370' in s
    assert "open_unrealized_pnl=float(_open_pnl or 0.0)" in s


def test_manual_buy_blocked_when_realized_plus_open_loss_hits_the_cap():
    """The engine step (E368) fed by the manual path: a net loss at or over the cap blocks a BUY; a SELL is never blocked."""
    import sys, types
    sys.path.insert(0, str(ROOT))
    from risk_engine.engine import AccountState, OrderIntent, RiskVerdict, evaluate
    import dataclasses
    base = AccountState(equity=100000.0, risk_per_trade_pct=1.0, max_daily_loss_pct=3.0, max_concurrent_positions=5,
                        max_portfolio_risk_pct=10.0, stale_data_seconds=60, max_tick_volatility_mult=3.0,
                        allow_pyramiding=True, realized_pnl_today=-500.0, open_position_count=1, open_position_symbols={"X"},
                        open_positions_total_risk=0.0, trading_globally_paused=False, market_is_open=True)
    buy = OrderIntent(mode="REAL", symbol="ABC", side="BUY", qty=1, entry_price=100.0, stop_price=97.0)
    r = evaluate(buy, dataclasses.replace(base, open_unrealized_pnl=-2600.0))
    assert r.verdict == RiskVerdict.BLOCKED_GLOBAL and r.check_name == "daily_loss_limit_open"
    sell = OrderIntent(mode="REAL", symbol="X", side="SELL", qty=1, entry_price=100.0, stop_price=97.0)
    assert evaluate(sell, dataclasses.replace(base, open_unrealized_pnl=-2600.0)).check_name != "daily_loss_limit_open"
