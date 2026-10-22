"""A11 (2026-10-22): account-wide daily loss cap - risk_engine check 3a + execution/shared_daily_pnl.py.

Run from services/real-trade-service:  python3 -m pytest tests/test_a11_account_daily_loss.py -q
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

from risk_engine import engine
from risk_engine.engine import AccountState, OrderIntent, RiskVerdict, evaluate

NOW = datetime(2026, 10, 9, 5, 0, 0, tzinfo=timezone.utc)  # 10:30 IST, market hours


@pytest.fixture(autouse=True)
def _pin(monkeypatch):
    monkeypatch.setattr(engine, "MIN_STOCK_PRICE", 20.0)
    monkeypatch.setattr(engine, "MAX_STOCK_PRICE", 3000.0)
    monkeypatch.setattr(engine, "MAX_STOCK_PRICE_EXPLICITLY_SET", False)
    monkeypatch.setattr(engine, "RISK_MAX_STOCK_PRICE_ADAPTIVE", True)
    monkeypatch.setattr(engine, "CAPITAL_SHARE_PCT", 50.0)
    monkeypatch.setattr(engine, "LOSS_STREAK_MAX", 3)
    monkeypatch.setattr(engine, "MAX_ENTRIES_PER_DAY", 6)
    monkeypatch.setattr(engine, "ACCOUNT_MAX_DAILY_LOSS_PCT", 3.0)


def _account(**over):
    base = dict(
        equity=100_000.0, risk_per_trade_pct=1.0, max_daily_loss_pct=3.0, max_concurrent_positions=3,
        max_portfolio_risk_pct=5.0, stale_data_seconds=30, max_tick_volatility_mult=2.0, allow_pyramiding=False,
        realized_pnl_today=0.0, open_position_count=0, open_position_symbols=set(), open_positions_total_risk=0.0,
        trading_globally_paused=False, market_is_open=True, cash_available=100_000.0,
        broker_cash_available=200_000.0, open_positions_market_value=0.0,
        other_service_open_positions_market_value=0.0,
    )
    base.update(over)
    return AccountState(**base)


def _intent(**over):
    base = dict(mode="REAL", symbol="TESTCO", side="BUY", qty=100, entry_price=100.0, stop_price=98.0)
    base.update(over)
    return OrderIntent(**base)


class TestEngineCheck:
    def test_blocks_buy_at_cap(self):
        r = evaluate(_intent(), _account(account_wide_daily_loss_pct=3.0), now=NOW)
        assert r.verdict == RiskVerdict.BLOCKED_GLOBAL
        assert r.check_name == "account_daily_loss_limit"
        assert "both engines" in r.reason

    def test_blocks_buy_above_cap(self):
        r = evaluate(_intent(), _account(account_wide_daily_loss_pct=4.2), now=NOW)
        assert r.check_name == "account_daily_loss_limit"

    def test_below_cap_does_not_trip_this_check(self):
        r = evaluate(_intent(), _account(account_wide_daily_loss_pct=2.99), now=NOW)
        assert r.check_name != "account_daily_loss_limit"

    def test_sell_is_never_blocked(self):
        r = evaluate(_intent(side="SELL"), _account(account_wide_daily_loss_pct=9.0), now=NOW)
        assert r.check_name != "account_daily_loss_limit"

    def test_cap_zero_disables(self, monkeypatch):
        monkeypatch.setattr(engine, "ACCOUNT_MAX_DAILY_LOSS_PCT", 0.0)
        r = evaluate(_intent(), _account(account_wide_daily_loss_pct=50.0), now=NOW)
        assert r.check_name != "account_daily_loss_limit"

    def test_unpopulated_default_never_triggers(self):
        assert _account().account_wide_daily_loss_pct == 0.0
        r = evaluate(_intent(), _account(), now=NOW)
        assert r.check_name != "account_daily_loss_limit"

    def test_per_service_cap_still_reports_first(self):
        # own loss 3.5% of equity trips the original check before the account-wide one
        r = evaluate(_intent(), _account(realized_pnl_today=-3_500.0, account_wide_daily_loss_pct=5.0), now=NOW)
        assert r.check_name == "daily_loss_limit"

    def test_global_pause_still_reports_before(self):
        r = evaluate(_intent(), _account(trading_globally_paused=True, account_wide_daily_loss_pct=5.0), now=NOW)
        assert r.check_name == "global_pause"


class TestMath:
    def _mod(self):
        from execution import shared_daily_pnl
        return shared_daily_pnl

    def test_both_lose(self):
        # -1500 on 50k and -2000 on 50k = -3500 / 100k = 3.5%
        assert round(self._mod().combined_loss_pct(-1500, 50_000, -2000, 50_000), 4) == 3.5

    def test_profit_offsets_loss(self):
        assert round(self._mod().combined_loss_pct(-3000, 50_000, 1000, 50_000), 4) == 2.0

    def test_net_profit_is_zero(self):
        assert self._mod().combined_loss_pct(500, 50_000, 200, 50_000) == 0.0

    def test_no_base_is_zero(self):
        assert self._mod().combined_loss_pct(-1000, 0, -1000, 0) == 0.0

    def test_missing_peer_falls_back_to_own(self):
        assert round(self._mod().combined_loss_pct(-1500, 50_000, 0.0, 0.0), 4) == 3.0


@pytest.fixture()
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import models
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


class TestSharedRow:
    def test_publish_then_peer_view(self, db):
        import models
        from execution import shared_daily_pnl as m
        m.publish_own(db, -1200.0, 50_000.0)
        row = db.query(models.SharedDailyPnl).filter_by(service_name=m.SERVICE_NAME).one()
        assert row.realized_pnl_today == -1200.0 and row.capital_base == 50_000.0
        # own row must not count as the peer
        assert m.get_peer(db) == (0.0, 0.0, None)

    def test_peer_today_is_counted(self, db):
        import models
        from execution import shared_daily_pnl as m
        from tz_utils import ist_today_str
        db.add(models.SharedDailyPnl(service_name=m.OTHER_SERVICE_NAME, trade_date=ist_today_str(),
                                     realized_pnl_today=-2000.0, capital_base=50_000.0))
        db.commit()
        pnl, base, age = m.get_peer(db)
        assert (pnl, base) == (-2000.0, 50_000.0) and age is not None
        assert round(m.account_wide_loss_pct(db, -1500.0, 50_000.0), 4) == 3.5

    def test_peer_from_earlier_day_is_ignored(self, db):
        import models
        from execution import shared_daily_pnl as m
        db.add(models.SharedDailyPnl(service_name=m.OTHER_SERVICE_NAME, trade_date="2000-01-01",
                                     realized_pnl_today=-99_999.0, capital_base=50_000.0))
        db.commit()
        assert m.get_peer(db) == (0.0, 0.0, None)
        assert round(m.account_wide_loss_pct(db, -1000.0, 50_000.0), 4) == 2.0

    def test_publish_overwrites_same_row(self, db):
        import models
        from execution import shared_daily_pnl as m
        m.publish_own(db, -100.0, 10.0)
        m.publish_own(db, -300.0, 20.0)
        rows = db.query(models.SharedDailyPnl).all()
        assert len(rows) == 1 and rows[0].realized_pnl_today == -300.0 and rows[0].capital_base == 20.0

    def test_db_errors_are_swallowed(self):
        from execution import shared_daily_pnl as m

        class Boom:
            def query(self, *a, **k):
                raise RuntimeError("db down")
            def rollback(self):
                raise RuntimeError("still down")
        assert m.get_peer(Boom()) == (0.0, 0.0, None)
        m.publish_own(Boom(), -1.0, 1.0)  # must not raise
        assert round(m.account_wide_loss_pct(Boom(), -1500.0, 50_000.0), 4) == 3.0
