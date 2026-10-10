"""group 289: loss-streak brake for real-trade-service
(risk_engine check #3b + portfolio.today_loss_streak + the wiring in entry_engine/entry.py).

Run from services/real-trade-service:  python3 -m pytest tests/test_group289_loss_streak.py -q
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import portfolio.portfolio as pf
from entry_engine import entry
from execution import equity_sync
from risk_engine import engine
from risk_engine.engine import AccountState, OrderIntent, RiskVerdict, evaluate
from tz_utils import ist_today_str

NOW = datetime(2026, 10, 9, 5, 0, 0, tzinfo=timezone.utc)  # 10:30 IST, market hours


@pytest.fixture(autouse=True)
def _pin(monkeypatch):
    monkeypatch.setattr(engine, "MIN_STOCK_PRICE", 20.0)
    monkeypatch.setattr(engine, "MAX_STOCK_PRICE", 3000.0)
    monkeypatch.setattr(engine, "MAX_STOCK_PRICE_EXPLICITLY_SET", False)
    monkeypatch.setattr(engine, "RISK_MAX_STOCK_PRICE_ADAPTIVE", True)
    monkeypatch.setattr(engine, "CAPITAL_SHARE_PCT", 50.0)
    monkeypatch.setattr(engine, "LOSS_STREAK_MAX", 3)
    monkeypatch.setattr(engine, "LOSS_STREAK_PAUSE_MINUTES", 45)


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


# ── engine: check #3b ────────────────────────────────────────────────────────

class TestEngineLossStreak:
    def test_default_account_state_has_no_brake(self):
        assert evaluate(_intent(), _account(), now=NOW).verdict == RiskVerdict.APPROVED

    @pytest.mark.parametrize("streak", [0, 1, 2])
    def test_streak_below_limit_passes(self, streak):
        res = evaluate(_intent(), _account(loss_streak=streak, loss_streak_last_close_at=NOW - timedelta(minutes=1)), now=NOW)
        assert res.verdict == RiskVerdict.APPROVED

    def test_streak_at_limit_inside_pause_blocks_buy(self):
        res = evaluate(_intent(), _account(loss_streak=3, loss_streak_last_close_at=NOW - timedelta(minutes=10)), now=NOW)
        assert res.verdict == RiskVerdict.REJECTED
        assert res.check_name == "loss_streak_pause"
        assert res.approved_qty is None
        assert "35 more min" in res.reason

    def test_pause_ends_exactly_at_the_cooldown(self):
        res = evaluate(_intent(), _account(loss_streak=5, loss_streak_last_close_at=NOW - timedelta(minutes=45)), now=NOW)
        assert res.verdict == RiskVerdict.APPROVED

    def test_one_minute_before_the_cooldown_still_blocks(self):
        res = evaluate(_intent(), _account(loss_streak=3, loss_streak_last_close_at=NOW - timedelta(minutes=44)), now=NOW)
        assert res.check_name == "loss_streak_pause"

    def test_naive_close_time_is_read_as_utc(self):
        naive = (NOW - timedelta(minutes=5)).replace(tzinfo=None)
        res = evaluate(_intent(), _account(loss_streak=3, loss_streak_last_close_at=naive), now=NOW)
        assert res.check_name == "loss_streak_pause"

    def test_close_time_in_ist_offset_is_compared_correctly(self):
        ist = timezone(timedelta(hours=5, minutes=30))
        res = evaluate(_intent(), _account(loss_streak=3, loss_streak_last_close_at=(NOW - timedelta(minutes=5)).astimezone(ist)), now=NOW)
        assert res.check_name == "loss_streak_pause" and "40 more min" in res.reason

    def test_sell_is_never_blocked(self):
        res = evaluate(_intent(side="SELL"), _account(loss_streak=9, loss_streak_last_close_at=NOW), now=NOW)
        assert res.verdict == RiskVerdict.APPROVED

    def test_streak_without_a_close_time_does_not_block(self):
        res = evaluate(_intent(), _account(loss_streak=9, loss_streak_last_close_at=None), now=NOW)
        assert res.verdict == RiskVerdict.APPROVED

    def test_limit_zero_disables(self, monkeypatch):
        monkeypatch.setattr(engine, "LOSS_STREAK_MAX", 0)
        res = evaluate(_intent(), _account(loss_streak=9, loss_streak_last_close_at=NOW), now=NOW)
        assert res.verdict == RiskVerdict.APPROVED

    def test_zero_pause_minutes_never_blocks(self, monkeypatch):
        monkeypatch.setattr(engine, "LOSS_STREAK_PAUSE_MINUTES", 0)
        res = evaluate(_intent(), _account(loss_streak=9, loss_streak_last_close_at=NOW), now=NOW)
        assert res.verdict == RiskVerdict.APPROVED

    def test_negative_pause_minutes_never_blocks(self, monkeypatch):
        monkeypatch.setattr(engine, "LOSS_STREAK_PAUSE_MINUTES", -30)
        res = evaluate(_intent(), _account(loss_streak=9, loss_streak_last_close_at=NOW), now=NOW)
        assert res.verdict == RiskVerdict.APPROVED

    def test_daily_loss_cap_still_wins_when_both_apply(self):
        res = evaluate(_intent(), _account(realized_pnl_today=-3_000.0, loss_streak=3,
                                           loss_streak_last_close_at=NOW), now=NOW)
        assert res.check_name == "daily_loss_limit"

    def test_global_pause_and_market_closed_still_win(self):
        a = _account(loss_streak=3, loss_streak_last_close_at=NOW, trading_globally_paused=True)
        assert evaluate(_intent(), a, now=NOW).check_name == "global_pause"
        a = _account(loss_streak=3, loss_streak_last_close_at=NOW, market_is_open=False)
        assert evaluate(_intent(), a, now=NOW).check_name == "market_closed"

    def test_brake_runs_before_the_sizing_checks(self):
        # a BUY that would also fail concurrent positions reports the streak first (cheaper, more useful reason)
        res = evaluate(_intent(), _account(open_position_count=3, loss_streak=3,
                                           loss_streak_last_close_at=NOW), now=NOW)
        assert res.check_name == "loss_streak_pause"

    def test_blank_env_gives_default(self, monkeypatch):
        monkeypatch.setenv("RISK_LOSS_STREAK_MAX", "  ")
        monkeypatch.setenv("RISK_LOSS_STREAK_PAUSE_MINUTES", "abc")
        assert engine._blank_safe_int("RISK_LOSS_STREAK_MAX", 3) == 3
        assert engine._blank_safe_int("RISK_LOSS_STREAK_PAUSE_MINUTES", 45) == 45
        monkeypatch.setenv("RISK_LOSS_STREAK_MAX", "4")
        assert engine._blank_safe_int("RISK_LOSS_STREAK_MAX", 3) == 4

    def test_unset_env_gives_default(self, monkeypatch):
        monkeypatch.delenv("RISK_LOSS_STREAK_MAX", raising=False)
        assert engine._blank_safe_int("RISK_LOSS_STREAK_MAX", 3) == 3


# ── portfolio.today_loss_streak (real SQLite) ────────────────────────────────

@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _closed(db, pnl, minutes_ago, *, mode="REAL", imported=False, status="CLOSED", symbol="X"):
    closed = (NOW - timedelta(minutes=minutes_ago)).replace(tzinfo=None)  # DB stores naive UTC
    db.add(models.TradePosition(
        mode=mode, symbol=symbol, status=status, qty_open=0, avg_entry_price=100.0,
        opened_at=closed - timedelta(minutes=20), closed_at=closed, realized_pnl=pnl, broker_imported=imported,
    ))
    db.commit()


class TestTodayLossStreak:
    def test_no_rows(self, db):
        assert pf.today_loss_streak(db, "REAL", NOW) == (0, None)

    def test_counts_newest_consecutive_losses_and_returns_latest_close(self, db):
        _closed(db, +50, 90)
        _closed(db, -10, 60)
        _closed(db, -20, 30)
        _closed(db, -5, 10)
        streak, last = pf.today_loss_streak(db, "REAL", NOW)
        assert streak == 3
        assert last == (NOW - timedelta(minutes=10)).replace(tzinfo=None)

    def test_a_win_ends_the_streak(self, db):
        _closed(db, -10, 60)
        _closed(db, -10, 40)
        _closed(db, +5, 20)
        assert pf.today_loss_streak(db, "REAL", NOW)[0] == 0

    def test_zero_pnl_close_ends_the_streak(self, db):
        _closed(db, -10, 60)
        _closed(db, 0.0, 20)
        assert pf.today_loss_streak(db, "REAL", NOW)[0] == 0

    def test_unknown_pnl_ends_the_streak(self, db):
        _closed(db, -10, 60)
        _closed(db, None, 20)
        assert pf.today_loss_streak(db, "REAL", NOW)[0] == 0

    def test_yesterday_and_other_mode_are_ignored(self, db):
        _closed(db, -10, 60 * 24)          # yesterday IST
        _closed(db, -10, 5, mode="DEMO")
        assert pf.today_loss_streak(db, "REAL", NOW) == (0, None)

    def test_ist_midnight_boundary(self, db):
        # NOW is 10:30 IST on 9 Oct, so IST midnight = 8 Oct 18:30 UTC = 10.5 h before NOW.
        _closed(db, -10, 10 * 60 + 29)     # 00:01 IST today: counts
        _closed(db, -10, 10 * 60 + 31)     # 23:59 IST yesterday: does not
        assert pf.today_loss_streak(db, "REAL", NOW)[0] == 1

    def test_imported_holdings_and_open_rows_are_skipped(self, db):
        _closed(db, -200, 5, imported=True)
        _closed(db, -10, 8, status="OPEN")
        _closed(db, -10, 12)
        assert pf.today_loss_streak(db, "REAL", NOW)[0] == 1

    def test_imported_holding_does_not_break_a_streak_either(self, db):
        _closed(db, -10, 30)
        _closed(db, +300, 20, imported=True)   # an imported winner is not "today's entry" and must not reset it
        _closed(db, -10, 10)
        assert pf.today_loss_streak(db, "REAL", NOW)[0] == 2

    def test_db_error_fails_open(self):
        class Boom:
            def query(self, *a, **k):
                raise RuntimeError("db down")
        assert pf.today_loss_streak(Boom(), "REAL", NOW) == (0, None)


# ── wiring: entry._account_state -> evaluate ────────────────────────────────

@pytest.fixture()
def acct(db, monkeypatch):
    monkeypatch.setattr(equity_sync, "sync_real_equity", lambda d: None)
    monkeypatch.setattr(entry, "is_market_open_ist", lambda: True)
    monkeypatch.setattr(entry.shared_exposure, "get_other_service_exposure", lambda d: 0.0)
    # pin the clock the real helper sees, so this can never flake around IST midnight
    real = pf.today_loss_streak
    monkeypatch.setattr(entry, "today_loss_streak", lambda d, m, now=None: real(d, m, NOW))
    for mode in ("REAL", "DEMO"):
        db.add(models.TradeAccount(mode=mode, starting_capital=100_000.0, current_equity=100_000.0,
                                   cash_available=100_000.0, broker_cash_available=200_000.0,
                                   realized_pnl_today=-30.0, realized_pnl_total=0.0,
                                   pnl_last_reset_date=ist_today_str()))
        db.add(models.TradeRiskConfig(mode=mode, risk_per_trade_pct=1.0, max_daily_loss_pct=3.0,
                                      max_concurrent_positions=3, max_portfolio_risk_pct=5.0,
                                      stale_data_seconds=30, max_tick_volatility_mult=2.0,
                                      allow_pyramiding=False))
    db.commit()


class TestEntryWiring:
    def test_account_state_carries_the_streak(self, db, acct):
        _closed(db, -10, 40)
        _closed(db, -20, 25)
        _closed(db, -5, 10)
        a = entry._account_state(db, "REAL", gate_armed=True)
        assert a.loss_streak == 3
        assert a.loss_streak_last_close_at == (NOW - timedelta(minutes=10)).replace(tzinfo=None)

    def test_three_losses_block_the_next_buy_end_to_end(self, db, acct):
        _closed(db, -10, 40)
        _closed(db, -20, 25)
        _closed(db, -5, 10)
        res = evaluate(_intent(), entry._account_state(db, "REAL", gate_armed=True), now=NOW)
        assert res.check_name == "loss_streak_pause" and res.verdict == RiskVerdict.REJECTED

    def test_a_win_in_between_lets_the_buy_through_end_to_end(self, db, acct):
        _closed(db, -10, 40)
        _closed(db, +25, 25)
        _closed(db, -5, 10)
        res = evaluate(_intent(), entry._account_state(db, "REAL", gate_armed=True), now=NOW)
        assert res.verdict == RiskVerdict.APPROVED

    def test_demo_streak_does_not_brake_real(self, db, acct):
        for m in (40, 25, 10):
            _closed(db, -10, m, mode="DEMO")
        assert entry._account_state(db, "REAL", gate_armed=True).loss_streak == 0
        assert entry._account_state(db, "DEMO", gate_armed=True).loss_streak == 3

    def test_a_failing_streak_query_never_breaks_account_state(self, db, acct, monkeypatch):
        # today_loss_streak swallows its own errors, so the account state still builds with no brake
        monkeypatch.setattr(entry, "today_loss_streak", lambda d, m, now=None: (0, None))
        assert entry._account_state(db, "REAL", gate_armed=True).loss_streak == 0
