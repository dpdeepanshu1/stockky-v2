"""
E194 - exit_engine.exit.evaluate_mode: a REAL exit whose SELL is blocked (rejection backoff, or today's intraday cutoff) wrote a new
FULL_EXIT / EMERGENCY_EXIT audit row on every ~8 s cycle, although nothing was sent. Only HOLD rows were throttled (group368).

Run from services/real-trade-service:
    python3 -m pytest tests/test_e194_blocked_exit_audit_rows.py -q
"""
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
import exit_engine.exit as ex
from market_feed.feed import Tick
from resilience.local_cache import save_snapshot
from tz_utils import ist_today_str


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _throttle_on(monkeypatch):
    monkeypatch.setattr(config, "EXIT_HOLD_AUDIT_SECONDS", 60, raising=False)
    ex._HOLD_AUDIT_LAST.clear()
    for name in ("_BLOCKED_EXIT_AUDIT_LAST",):
        if hasattr(ex, name):
            getattr(ex, name).clear()
    # a blocked SELL must never reach Dhan in these tests
    def _no_broker(*a, **k):
        raise AssertionError("the broker must not be called while the exit is blocked")
    monkeypatch.setattr(ex.dhan_client, "place_order", _no_broker)
    yield


def make_real(db, *, symbol="TESTCO", qty=10, entry=100.0, stop=95.0, target=110.0, failures=0, last_failure=None):
    pos = models.TradePosition(
        mode="REAL", symbol=symbol, status="OPEN", qty_open=qty, avg_entry_price=entry,
        current_stop=stop, current_target=target, initial_stop_distance=entry - stop,
        opened_at=datetime.now(timezone.utc) - timedelta(days=1),
        consecutive_exit_failures=failures, last_exit_failure_at=last_failure,
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)
    return pos


def quotes(monkeypatch, price, symbol="TESTCO"):
    async def _q(symbols, **_kw):
        return {symbol: Tick(symbol=symbol, price=price, as_of=datetime.now(timezone.utc), atr=None, source="test")}
    monkeypatch.setattr(ex, "get_quotes", _q)


def rows(db, position, action):
    return db.query(models.TradeExitDecision).filter_by(position_id=position.id, action=action).count()


def cycles(db, n):
    for _ in range(n):
        run(ex.evaluate_mode(db, "REAL"))


class TestBlockedExitIsAuditedOnce:
    def test_stop_hit_in_backoff_writes_one_row_for_many_cycles(self, db, monkeypatch):
        pos = make_real(db, failures=3, last_failure=datetime.now(timezone.utc))
        quotes(monkeypatch, 94.0)
        cycles(db, 6)
        assert rows(db, pos, "FULL_EXIT") == 1

    def test_emergency_in_backoff_writes_one_row(self, db, monkeypatch):
        pos = make_real(db, stop=95.0, failures=2, last_failure=datetime.now(timezone.utc))
        quotes(monkeypatch, 80.0)   # far through the stop: emergency branch
        cycles(db, 5)
        assert rows(db, pos, "EMERGENCY_EXIT") == 1

    def test_partial_target_in_backoff_writes_one_row(self, db, monkeypatch):
        pos = make_real(db, failures=1, last_failure=datetime.now(timezone.utc))
        quotes(monkeypatch, 111.0)
        cycles(db, 4)
        assert rows(db, pos, "PARTIAL_EXIT") == 1

    def test_intraday_cutoff_snapshot_counts_as_blocked(self, db, monkeypatch):
        pos = make_real(db)
        save_snapshot(db, f"intraday_cutoff_hit_{pos.id}_{ist_today_str()}", {"hit": True})
        quotes(monkeypatch, 94.0)
        cycles(db, 5)
        assert rows(db, pos, "FULL_EXIT") == 1

    def test_blocked_exit_is_still_not_sent(self, db, monkeypatch):
        pos = make_real(db, failures=3, last_failure=datetime.now(timezone.utc))
        quotes(monkeypatch, 94.0)
        tally = run(ex.evaluate_mode(db, "REAL"))
        assert tally["full_exits"] == 0 and tally["held"] == 1
        db.refresh(pos)
        assert pos.status == "OPEN"


class TestNothingElseChanges:
    def test_exit_that_is_attempted_every_cycle_still_has_a_row_each_time(self, db, monkeypatch):
        """No backoff and no cutoff: every cycle really tries to sell, so every cycle keeps its row."""
        pos = make_real(db)
        calls = []
        monkeypatch.setattr(ex, "_send_real_sell", lambda *a, **k: calls.append(1) or False)
        quotes(monkeypatch, 94.0)
        cycles(db, 3)
        assert len(calls) == 3
        assert rows(db, pos, "FULL_EXIT") == 3

    def test_row_is_written_when_the_backoff_ends_and_the_sell_goes_out(self, db, monkeypatch):
        pos = make_real(db, failures=2, last_failure=datetime.now(timezone.utc))
        quotes(monkeypatch, 94.0)
        cycles(db, 3)
        assert rows(db, pos, "FULL_EXIT") == 1
        pos.last_exit_failure_at = datetime.now(timezone.utc) - timedelta(hours=1)   # cooldown over
        db.commit()
        sent = []
        monkeypatch.setattr(ex, "_send_real_sell", lambda db_, p, qty, reason, **k: sent.append(reason) or True)
        cycles(db, 1)
        assert sent == ["stop_hit"]
        assert rows(db, pos, "FULL_EXIT") == 2

    def test_throttle_off_keeps_every_row(self, db, monkeypatch):
        monkeypatch.setattr(config, "EXIT_HOLD_AUDIT_SECONDS", 0, raising=False)
        pos = make_real(db, failures=3, last_failure=datetime.now(timezone.utc))
        quotes(monkeypatch, 94.0)
        cycles(db, 4)
        assert rows(db, pos, "FULL_EXIT") == 4

    def test_each_position_has_its_own_throttle(self, db, monkeypatch):
        a = make_real(db, symbol="AAA", failures=3, last_failure=datetime.now(timezone.utc))
        b = make_real(db, symbol="BBB", failures=3, last_failure=datetime.now(timezone.utc))

        async def _q(symbols, **_kw):
            now = datetime.now(timezone.utc)
            return {s: Tick(symbol=s, price=94.0, as_of=now, atr=None, source="test") for s in symbols}
        monkeypatch.setattr(ex, "get_quotes", _q)
        cycles(db, 3)
        assert rows(db, a, "FULL_EXIT") == 1
        assert rows(db, b, "FULL_EXIT") == 1

    def test_a_different_action_is_not_swallowed(self, db, monkeypatch):
        pos = make_real(db, stop=95.0, failures=3, last_failure=datetime.now(timezone.utc))
        quotes(monkeypatch, 94.0)
        cycles(db, 2)
        quotes(monkeypatch, 70.0)   # now an emergency, a different decision
        cycles(db, 2)
        assert rows(db, pos, "FULL_EXIT") == 1
        assert rows(db, pos, "EMERGENCY_EXIT") == 1

    def test_demo_positions_are_never_throttled(self, db, monkeypatch):
        db.add(models.TradeAccount(mode="DEMO", starting_capital=100000.0, current_equity=100000.0, cash_available=100000.0))
        db.commit()
        p = models.TradePosition(mode="DEMO", symbol="TESTCO", status="OPEN", qty_open=10, avg_entry_price=100.0,
                                 current_stop=95.0, current_target=110.0, initial_stop_distance=5.0,
                                 opened_at=datetime.now(timezone.utc) - timedelta(days=1),
                                 consecutive_exit_failures=3, last_exit_failure_at=datetime.now(timezone.utc))
        db.add(p)
        db.commit()
        quotes(monkeypatch, 94.0)
        run(ex.evaluate_mode(db, "DEMO"))
        assert rows(db, p, "FULL_EXIT") == 1
        assert db.query(models.TradePosition).filter_by(id=p.id).first().status == "CLOSED"


class TestHelper:
    def test_helper_false_for_unblocked_and_true_for_backoff(self, db):
        free = make_real(db, symbol="FREE")
        busy = make_real(db, symbol="BUSY", failures=2, last_failure=datetime.now(timezone.utc))
        assert ex._exit_send_is_blocked(db, free) is False
        assert ex._exit_send_is_blocked(db, busy) is True

    def test_helper_never_raises(self, db):
        class Broken:
            id = 1
            mode = "REAL"
            consecutive_exit_failures = "x"
            last_exit_failure_at = object()
        assert ex._exit_send_is_blocked(db, Broken()) is False
