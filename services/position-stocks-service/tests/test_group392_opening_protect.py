"""group 392: opening protection (orders/opening_protect.py) - half size and a pause after stop-outs in 09:15-09:45."""
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
SAT = lambda h, m: datetime(2026, 10, 10, h, m, tzinfo=IST)
_ids = itertools.count(1)


@pytest.fixture(autouse=True)
def on(monkeypatch):
    monkeypatch.setattr(config, "OPENING_PROTECT_ENABLED", True)
    monkeypatch.setattr(config, "OPENING_PROTECT_UNTIL_IST", "09:45")
    monkeypatch.setattr(config, "OPENING_SIZE_MULT", 0.5)
    monkeypatch.setattr(config, "OPENING_PAUSE_STOPOUTS", 2)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def closed(db, status, at):
    n = next(_ids)
    db.add(models.ScalpPosition(symbol=f"G392{n}", dhan_security_id=str(n), window_source="5m", status=status, entry_price=100.0,
                                quantity=1, target_price=104.0, stop_price=99.0, adaptive_target_pct=4.0, adaptive_stop_pct=1.0,
                                capital_risked=100.0, closed_at=at.astimezone(timezone.utc)))
    db.commit()


class TestWindow:
    def test_active_only_inside_window_on_trading_days(self):
        assert opening_protect.active(MON(9, 15)) and opening_protect.active(MON(9, 44))
        assert not opening_protect.active(MON(9, 14)) and not opening_protect.active(MON(9, 45))
        assert not opening_protect.active(SAT(9, 30))

    def test_disabled(self, monkeypatch):
        monkeypatch.setattr(config, "OPENING_PROTECT_ENABLED", False)
        assert not opening_protect.active(MON(9, 30)) and opening_protect.size_mult(MON(9, 30)) == 1.0


class TestSizeMult:
    def test_half_inside_full_outside(self):
        assert opening_protect.size_mult(MON(9, 20)) == 0.5
        assert opening_protect.size_mult(MON(9, 50)) == 1.0

    def test_clamped(self, monkeypatch):
        monkeypatch.setattr(config, "OPENING_SIZE_MULT", 9.0)
        assert opening_protect.size_mult(MON(9, 20)) == 1.0
        monkeypatch.setattr(config, "OPENING_SIZE_MULT", -1.0)
        assert opening_protect.size_mult(MON(9, 20)) == 0.1


class TestPause:
    def test_two_stopouts_pause_until_window_end(self, db):
        closed(db, "STOP_HIT", MON(9, 18))
        assert opening_protect.pause_reason(db, MON(9, 25)) is None
        closed(db, "STOP_BREACH_EXIT", MON(9, 22))
        r = opening_protect.pause_reason(db, MON(9, 25))
        assert r and r.startswith("OPENING_PROTECT:PAUSED:2") and "09:45" in r

    def test_pause_ends_with_the_window(self, db):
        closed(db, "STOP_HIT", MON(9, 18)); closed(db, "STOP_HIT", MON(9, 20))
        assert opening_protect.pause_reason(db, MON(9, 45)) is None

    def test_targets_old_stops_and_other_statuses_do_not_count(self, db):
        closed(db, "TARGET_HIT", MON(9, 20))
        closed(db, "EOD_SQUAREOFF", MON(9, 21))
        closed(db, "STOP_HIT", MON(9, 10))               # before the bell
        closed(db, "STOP_HIT", MON(9, 30) - timedelta(days=1))
        assert opening_protect.stopouts_since_open(db, MON(9, 30)) == 0
        assert opening_protect.pause_reason(db, MON(9, 30)) is None

    def test_zero_limit_means_no_pause(self, db, monkeypatch):
        monkeypatch.setattr(config, "OPENING_PAUSE_STOPOUTS", 0)
        closed(db, "STOP_HIT", MON(9, 18)); closed(db, "STOP_HIT", MON(9, 20))
        assert opening_protect.pause_reason(db, MON(9, 25)) is None

    def test_db_error_fails_open(self):
        class Boom:
            def query(self, *a, **k):
                raise RuntimeError("db down")
        assert opening_protect.pause_reason(Boom(), MON(9, 25)) is None
