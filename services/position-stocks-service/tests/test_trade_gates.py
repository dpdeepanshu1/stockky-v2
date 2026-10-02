"""tests/test_trade_gates.py — market filter, loss brake, loss re-entry block (2026-10-02)."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from orders import entry
from screening import trade_gates


@pytest.fixture
def db(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    for k, v in dict(MARKET_GATE_ENABLED=True, MARKET_GATE_MIN_NIFTY_CHANGE_PCT=-0.10,
                     MARKET_GATE_CACHE_TTL_S=120.0, LOSS_BRAKE_ENABLED=True,
                     LOSS_BRAKE_MAX_CONSECUTIVE_LOSSES=3, LOSS_BRAKE_COOLDOWN_MINUTES=60,
                     LOSS_BRAKE_DAILY_PCT_OF_POOL=1.5, SYMBOL_BLOCK_AFTER_LOSS_TODAY=True,
                     SYMBOL_REENTRY_COOLDOWN_MINUTES=30, SYMBOL_REENTRY_MIN_PULLBACK_PCT=1.0).items():
        monkeypatch.setattr(config, k, v)
    trade_gates._market_cache.update({"pct": None, "ts": 0.0})
    led = models.ScalpCapitalLedger(mode="REAL", total_allocated_capital=10_000.0, available_capital=10_000.0)
    s.add(led)
    s.commit()
    yield s
    s.close()


_n = [0]


def closed(db, symbol, pnl, mins_ago=1, status="STOP_HIT"):
    _n[0] += 1
    p = models.ScalpPosition(
        symbol=symbol, dhan_security_id=str(_n[0]), window_source="5m", status=status,
        entry_price=100.0, quantity=1, target_price=104.0, stop_price=98.0,
        adaptive_target_pct=4.0, adaptive_stop_pct=2.0, capital_risked=100.0,
        exit_price=100.0 + pnl, realized_pnl=pnl,
        closed_at=datetime.now(timezone.utc) - timedelta(minutes=mins_ago),
    )
    db.add(p)
    db.commit()
    return p


# ── loss brake ───────────────────────────────────────────────────────────────
def test_no_trades_allows(db):
    assert trade_gates.loss_brake_reject(db) is None


def test_two_losses_allowed_three_blocked(db):
    closed(db, "A", -10, 5); closed(db, "B", -10, 4)
    assert trade_gates.loss_brake_reject(db) is None
    closed(db, "C", -10, 3)
    r = trade_gates.loss_brake_reject(db)
    assert r.startswith("LOSS_BRAKE_STREAK:3")


def test_streak_pause_expires(db):
    for i, s in enumerate("ABC"):
        closed(db, s, -5, 70 - i)          # last loss closed 68 min ago > 60 min cooldown
    assert trade_gates.loss_brake_reject(db) is None


def test_win_breaks_streak(db):
    closed(db, "A", -10, 9); closed(db, "B", -10, 8); closed(db, "W", +5, 7, status="TARGET_HIT")
    closed(db, "C", -10, 6)
    assert trade_gates.loss_brake_reject(db) is None


def test_daily_loss_cap_blocks_all_day(db):
    closed(db, "A", -100, 300, status="STOP_HIT")
    closed(db, "W", +60, 200, status="TARGET_HIT")
    closed(db, "B", -120, 150)             # net -160 of 10,000 = 1.6% >= 1.5%
    r = trade_gates.loss_brake_reject(db)
    assert r.startswith("LOSS_BRAKE_DAILY")


def test_yesterday_ignored(db):
    for s in "ABC":
        closed(db, s, -500, mins_ago=60 * 30)
    assert trade_gates.loss_brake_reject(db) is None


def test_open_and_error_rows_ignored(db):
    for s in "ABC":
        closed(db, s, -500, 2, status="ERROR")
    assert trade_gates.loss_brake_reject(db) is None


def test_brake_disabled(db, monkeypatch):
    monkeypatch.setattr(config, "LOSS_BRAKE_ENABLED", False)
    for s in "ABC":
        closed(db, s, -500, 2)
    assert trade_gates.loss_brake_reject(db) is None


def test_brake_fails_open_on_db_error(db):
    class Boom:
        def query(self, *a, **k):
            raise RuntimeError("db down")
    assert trade_gates.loss_brake_reject(Boom()) is None


# ── re-entry after loss ──────────────────────────────────────────────────────
def test_reentry_blocked_after_loss_even_past_cooldown(db):
    closed(db, "GANDHAR", -15, mins_ago=120)
    r = entry._reentry_guard_reject(db, "GANDHAR", 50.0)   # far below exit: pullback rule would allow
    assert r.startswith("REENTRY_BLOCKED_AFTER_LOSS")


def test_reentry_after_win_uses_old_cooldown_rule(db):
    closed(db, "ABC", +10, mins_ago=5, status="TARGET_HIT")
    assert entry._reentry_guard_reject(db, "ABC", 110.0).startswith("REENTRY_COOLDOWN")
    assert entry._reentry_guard_reject(db, "ABC", 108.0) is None   # real pullback


def test_loss_block_other_symbol_unaffected(db):
    closed(db, "AAA", -15, 5)
    assert entry._reentry_guard_reject(db, "BBB", 100.0) is None


def test_loss_block_yesterday_does_not_apply(db):
    closed(db, "AAA", -15, mins_ago=60 * 30)
    assert entry._reentry_guard_reject(db, "AAA", 100.0) is None


def test_loss_block_toggle(db, monkeypatch):
    monkeypatch.setattr(config, "SYMBOL_BLOCK_AFTER_LOSS_TODAY", False)
    closed(db, "AAA", -15, 120)
    assert entry._reentry_guard_reject(db, "AAA", 100.0) is None


# ── market gate ──────────────────────────────────────────────────────────────
def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _patch_fetch(monkeypatch, value, counter=None):
    async def f():
        if counter is not None:
            counter.append(1)
        return value
    monkeypatch.setattr(trade_gates, "_fetch_nifty_change_pct", f)


def test_market_weak_blocks(db, monkeypatch):
    _patch_fetch(monkeypatch, -0.35)
    assert _run(trade_gates.market_gate_reject()).startswith("MARKET_WEAK")


def test_market_threshold_boundary(db, monkeypatch):
    _patch_fetch(monkeypatch, -0.10)
    assert _run(trade_gates.market_gate_reject()) is not None      # <= threshold blocks
    trade_gates._market_cache.update({"pct": None, "ts": 0.0})
    _patch_fetch(monkeypatch, -0.09)
    assert _run(trade_gates.market_gate_reject()) is None


def test_market_ok_allows(db, monkeypatch):
    _patch_fetch(monkeypatch, 0.4)
    assert _run(trade_gates.market_gate_reject()) is None


def test_market_fetch_failure_fails_open(db, monkeypatch):
    _patch_fetch(monkeypatch, None)
    assert _run(trade_gates.market_gate_reject()) is None


def test_market_cache_prevents_refetch(db, monkeypatch):
    calls = []
    _patch_fetch(monkeypatch, -0.5, calls)
    _run(trade_gates.market_gate_reject()); _run(trade_gates.market_gate_reject())
    assert len(calls) == 1


def test_market_disabled(db, monkeypatch):
    monkeypatch.setattr(config, "MARKET_GATE_ENABLED", False)
    _patch_fetch(monkeypatch, -2.0)
    assert _run(trade_gates.market_gate_reject()) is None


def test_last_nifty_exposed(db, monkeypatch):
    _patch_fetch(monkeypatch, 0.25)
    _run(trade_gates.market_gate_reject())
    assert trade_gates.last_nifty_change_pct() == 0.25


# ── pending-reconcile exits must not reset the streak (2026-10-02) ───────────
def pending(db, symbol, mins_ago, status="EOD_SQUAREOFF"):
    """A flat-SELL exit whose P&L is still the 0.0 placeholder."""
    p = closed(db, symbol, 0.0, mins_ago, status=status)
    p.error_message = f"{status}_PENDING_RECONCILE: exit_price=entry_price placeholder"
    db.commit()
    return p


def test_pending_exit_between_losses_does_not_reset_streak(db):
    closed(db, "A", -10, 9); closed(db, "B", -10, 8)
    pending(db, "P", 7, status="STAGNATION_EXIT")
    closed(db, "C", -10, 6)
    assert trade_gates.loss_brake_reject(db).startswith("LOSS_BRAKE_STREAK:3")


def test_pending_exit_as_newest_row_does_not_hide_the_streak(db):
    for i, s in enumerate("ABC"):
        closed(db, s, -10, 9 - i)
    pending(db, "P", 1, status="MANUAL_EXIT")
    assert trade_gates.loss_brake_reject(db).startswith("LOSS_BRAKE_STREAK:3")


def test_pause_is_timed_from_last_resolved_loss_not_the_pending_exit(db):
    # three losses closed ~68 min ago (cooldown over); a pending exit 1 min ago
    for i, s in enumerate("ABC"):
        closed(db, s, -5, 70 - i)
    pending(db, "P", 1)
    assert trade_gates.loss_brake_reject(db) is None


def test_resolved_zero_pnl_exit_still_breaks_streak(db):
    # once reconcile fills the real price the sentinel is cleared; a true
    # break-even exit is a real result and does break the streak
    closed(db, "A", -10, 9); closed(db, "B", -10, 8)
    closed(db, "Z", 0.0, 7)
    closed(db, "C", -10, 6)
    assert trade_gates.loss_brake_reject(db) is None


def test_only_pending_rows_allows(db):
    pending(db, "P", 3); pending(db, "Q", 2)
    assert trade_gates.loss_brake_reject(db) is None
