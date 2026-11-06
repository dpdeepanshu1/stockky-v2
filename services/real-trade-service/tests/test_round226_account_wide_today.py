"""round 226: the account-wide 'today' figure for the dashboard is own + peer realized, display only, never raises."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from execution import shared_daily_pnl as sdp
from tz_utils import ist_today_str


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setenv("ACCOUNT_WIDE_PEER_CACHE_S", "0")
    sdp._PEER_CACHE.update({"t": -1e12, "v": None})
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _peer(db, pnl, day=None):
    db.add(models.SharedDailyPnl(service_name=sdp.OTHER_SERVICE_NAME, trade_date=day or ist_today_str(),
                                 realized_pnl_today=pnl, capital_base=6000.0, updated_at=datetime.now(timezone.utc)))
    db.commit()


def test_sums_own_and_peer(db):
    _peer(db, -1.0)
    out = sdp.account_wide_today(db, 179.45)
    assert out["realized_today"] == 178.45 and out["own_realized_today"] == 179.45 and out["peer_realized_today"] == -1.0
    assert out["peer_available"] is True and out["includes_peer_open"] is False and out["peer_age_s"] is not None


def test_missing_peer_row_falls_back_to_own(db):
    out = sdp.account_wide_today(db, 50.0)
    assert out["realized_today"] == 50.0 and out["peer_available"] is False and out["peer_realized_today"] == 0.0


def test_peer_row_from_an_earlier_day_is_ignored(db):
    _peer(db, -500.0, day="2020-01-01")
    out = sdp.account_wide_today(db, 10.0)
    assert out["realized_today"] == 10.0 and out["peer_available"] is False


def test_none_own_value_and_db_failure_never_raise(db, monkeypatch):
    out = sdp.account_wide_today(db, None)
    assert out["realized_today"] == 0.0
    monkeypatch.setattr(sdp, "get_peer", lambda _db: (_ for _ in ()).throw(RuntimeError("boom")))
    out = sdp.account_wide_today(db, 7.0)
    assert out["realized_today"] == 7.0 and out["peer_available"] is False


def test_peer_is_cached_between_calls(db, monkeypatch):
    monkeypatch.setenv("ACCOUNT_WIDE_PEER_CACHE_S", "60")
    calls = []
    real = sdp.get_peer
    monkeypatch.setattr(sdp, "get_peer", lambda d: (calls.append(1), real(d))[1])
    _peer(db, 3.0)
    sdp.account_wide_today(db, 1.0)
    out = sdp.account_wide_today(db, 2.0)
    assert len(calls) == 1 and out["realized_today"] == 5.0
