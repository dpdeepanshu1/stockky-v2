"""E285 round 99: the opening window's two extra guards (orders/opening_protect.py) - at most OPENING_MAX_POSITIONS open at once, and a loss budget of
OPENING_LOSS_BUDGET_FRAC of the day's loss cap. Both are part of pause_reason(), so the long entry and the short entry (both call it) get them."""
from __future__ import annotations

import itertools
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from orders import opening_protect

IST = timezone(timedelta(hours=5, minutes=30))
MON = lambda h, m: datetime(2026, 10, 12, h, m, tzinfo=IST)
_ids = itertools.count(1)


@pytest.fixture(autouse=True)
def on(monkeypatch):
    monkeypatch.setattr(config, "OPENING_PROTECT_ENABLED", True)
    monkeypatch.setattr(config, "OPENING_PROTECT_UNTIL_IST", "09:45")
    monkeypatch.setattr(config, "OPENING_PAUSE_STOPOUTS", 2)
    monkeypatch.setattr(config, "OPENING_MAX_POSITIONS", 2)
    monkeypatch.setattr(config, "OPENING_LOSS_BUDGET_FRAC", 1.0 / 3.0)
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_PCT_OF_POOL", 4.0)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def pool(db, rupees):
    db.add(models.ScalpCapitalLedger(mode="REAL", total_allocated_capital=rupees, available_capital=rupees, realized_pnl_today=0.0, realized_pnl_total=0.0))
    db.commit()


def pos(db, status="OPEN", closed_at=None, net=None, realized=None):
    n = next(_ids)
    db.add(models.ScalpPosition(symbol=f"R99{n}", dhan_security_id=str(n), window_source="5m", status=status, entry_price=100.0, quantity=1,
                                target_price=104.0, stop_price=99.0, adaptive_target_pct=4.0, adaptive_stop_pct=1.0, capital_risked=100.0,
                                closed_at=closed_at.astimezone(timezone.utc) if closed_at else None, realized_pnl=(net if net is not None else realized)))
    db.commit()


class TestEarlyCap:
    def test_two_open_positions_stop_a_third_inside_the_window(self, db):
        pos(db)
        assert opening_protect.early_cap_reason(db, MON(9, 20)) is None
        pos(db)
        r = opening_protect.early_cap_reason(db, MON(9, 20))
        assert r and r.startswith("OPENING_PROTECT:MAX_POSITIONS:2 open")
        assert opening_protect.pause_reason(db, MON(9, 20)) == r

    def test_no_cap_after_the_window_or_when_closed_out(self, db):
        pos(db)
        pos(db)
        assert opening_protect.early_cap_reason(db, MON(9, 45)) is None
        pos(db, "TARGET_HIT", MON(9, 30), net=50.0)
        assert opening_protect.early_cap_reason(db, MON(9, 30)) is not None     # still two OPEN; the closed one does not count

    def test_exit_legs_rejected_counts_as_open(self, db):
        pos(db, "EXIT_LEGS_REJECTED")
        pos(db)
        assert opening_protect.early_cap_reason(db, MON(9, 20)) is not None

    def test_zero_means_no_cap_and_switch_off(self, db, monkeypatch):
        pos(db)
        pos(db)
        monkeypatch.setattr(config, "OPENING_MAX_POSITIONS", 0)
        assert opening_protect.early_cap_reason(db, MON(9, 20)) is None
        monkeypatch.setattr(config, "OPENING_MAX_POSITIONS", 2)
        monkeypatch.setattr(config, "OPENING_PROTECT_ENABLED", False)
        assert opening_protect.pause_reason(db, MON(9, 20)) is None

    def test_db_error_fails_open(self):
        class Boom:
            def query(self, *a, **k):
                raise RuntimeError("db down")
        assert opening_protect.early_cap_reason(Boom(), MON(9, 20)) is None
        assert opening_protect.loss_budget_reason(Boom(), MON(9, 20)) is None


class TestLossBudget:
    def test_budget_is_a_third_of_the_day_cap(self, db):
        pool(db, 100000.0)                                      # cap 4% = Rs 4,000, budget = Rs 1,333.33
        assert abs(opening_protect.loss_budget_rupees(db) - 4000.0 / 3.0) < 0.01

    def test_loss_at_the_budget_pauses_until_window_end(self, db):
        pool(db, 100000.0)
        pos(db, "STOP_HIT", MON(9, 25), net=-700.0)
        assert opening_protect.loss_budget_reason(db, MON(9, 30)) is None
        pos(db, "TARGET_HIT", MON(9, 28), net=100.0)
        assert opening_protect.loss_budget_reason(db, MON(9, 30)) is None      # net of winners: -600
        pos(db, "STOP_HIT", MON(9, 32), net=-800.0)                            # -1,400 <= -1,333
        r = opening_protect.loss_budget_reason(db, MON(9, 35))
        assert r and r.startswith("OPENING_PROTECT:LOSS_BUDGET:Rs1400") and "09:45" in r
        assert opening_protect.loss_budget_reason(db, MON(9, 45)) is None      # window over

    def test_falls_back_to_realized_pnl_and_ignores_yesterday_and_open_rows(self, db):
        pool(db, 100000.0)
        pos(db, "STOP_HIT", MON(9, 20), realized=-1500.0)
        assert opening_protect.loss_budget_reason(db, MON(9, 30)) is not None
        db2 = db
        pos(db2, "STOP_HIT", MON(9, 20) - timedelta(days=1), net=-9999.0)
        pos(db2, "OPEN")
        assert round(opening_protect.realized_since_open(db, MON(9, 30))) == -1500

    def test_unknown_pool_or_zero_switch_means_no_budget(self, db, monkeypatch):
        pos(db, "STOP_HIT", MON(9, 25), net=-5000.0)
        assert opening_protect.loss_budget_reason(db, MON(9, 30)) is None      # no ledger row: fails open, and the row is not created
        assert db.query(models.ScalpCapitalLedger).count() == 0
        pool(db, 100000.0)
        monkeypatch.setattr(config, "OPENING_LOSS_BUDGET_FRAC", 0.0)
        assert opening_protect.loss_budget_reason(db, MON(9, 30)) is None


def test_stopout_pause_still_comes_first(db):
    pool(db, 100000.0)
    pos(db, "STOP_HIT", MON(9, 20), net=-2000.0)
    pos(db, "STOP_BREACH_EXIT", MON(9, 22), net=-2000.0)
    assert opening_protect.pause_reason(db, MON(9, 30)).startswith("OPENING_PROTECT:PAUSED:2")


def test_both_entry_paths_call_pause_reason():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for f in ("orders/entry.py", "orders/short_live.py"):
        assert "opening_protect.pause_reason(db)" in open(os.path.join(root, f), encoding="utf-8").read(), f
