"""group 193: position-stocks-service publishes its open-position value from its own DB, regardless of Dhan
funds or the screening gates, so real-trade-service's 50% share-cap total never loses it."""
import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from capital import ledger, shared_exposure
from models import ScalpPosition, SharedServiceExposure


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(ledger, "_last_exposure_publish", {"value": None, "ts": 0.0})


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


@pytest.fixture()
def calls(monkeypatch):
    got = []
    monkeypatch.setattr(shared_exposure, "publish_own_exposure", lambda d, v: got.append(v))
    return got


def _pos(db, sym, status, cap):
    db.add(ScalpPosition(symbol=sym, dhan_security_id="1", window_source="5m", status=status, entry_price=100.0,
                         quantity=1, target_price=105.0, stop_price=98.0, adaptive_target_pct=5.0,
                         adaptive_stop_pct=2.0, capital_risked=cap))
    db.commit()


def test_publishes_open_and_stuck_exit_capital_only(db, calls):
    _pos(db, "A", "OPEN", 5_000.0)
    _pos(db, "B", "EXIT_LEGS_REJECTED", 3_000.0)
    _pos(db, "C", "TARGET_HIT", 9_999.0)
    ledger.publish_exposure(db)
    assert calls == [pytest.approx(8_000.0)]


def test_unchanged_value_is_not_republished_within_the_interval(db, calls):
    ledger.publish_exposure(db)
    ledger.publish_exposure(db)
    assert len(calls) == 1


def test_changed_value_publishes_immediately(db, calls):
    ledger.publish_exposure(db)
    _pos(db, "A", "OPEN", 1_000.0)
    ledger.publish_exposure(db)
    assert calls == [0.0, pytest.approx(1_000.0)]


def test_heartbeat_after_the_interval_and_force(db, calls, monkeypatch):
    ledger.publish_exposure(db)
    ledger._last_exposure_publish["ts"] -= ledger._EXPOSURE_PUBLISH_MIN_INTERVAL_S + 1
    ledger.publish_exposure(db)
    ledger.publish_exposure(db, force=True)
    assert len(calls) == 3


def test_failure_is_fail_open(db, monkeypatch, caplog):
    def boom(d, v):
        raise RuntimeError("db down")
    monkeypatch.setattr(shared_exposure, "publish_own_exposure", boom)
    with caplog.at_level(logging.WARNING, logger="position-stocks-ledger"):
        ledger.publish_exposure(db)
    assert "publish_exposure failed" in caplog.text


def test_publish_survives_a_failed_funds_read(db, calls, monkeypatch):
    from execution import dhan_client
    _pos(db, "A", "OPEN", 4_000.0)

    def boom(d):
        raise RuntimeError("network down")
    monkeypatch.setattr(dhan_client, "get_funds", boom)
    assert ledger.sync_from_broker(db) == 0.0
    assert calls == [pytest.approx(4_000.0)]


def test_publish_sets_a_heartbeat_even_when_value_unchanged(db):
    shared_exposure.publish_own_exposure(db, 100.0)
    row = db.query(SharedServiceExposure).filter_by(service_name=shared_exposure.SERVICE_NAME).first()
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    row.updated_at = old
    db.commit()
    shared_exposure.publish_own_exposure(db, 100.0)
    row = db.query(SharedServiceExposure).filter_by(service_name=shared_exposure.SERVICE_NAME).first()
    ts = row.updated_at if row.updated_at.tzinfo else row.updated_at.replace(tzinfo=timezone.utc)
    assert ts > old + timedelta(hours=1)
