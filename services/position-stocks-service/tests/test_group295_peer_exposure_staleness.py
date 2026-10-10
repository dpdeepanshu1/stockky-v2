"""group295: the scalp pool does not credit real-trade-service's exposure figure once it is stale."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from capital import ledger, shared_exposure
from execution import dhan_client


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(ledger, "_last_balance_key", None)
    monkeypatch.setattr(ledger, "_sync_log_state", {})
    monkeypatch.delenv("SCALP_POOL_CREDIT_PEER_EXPOSURE", raising=False)
    monkeypatch.delenv("SCALP_POOL_PEER_EXPOSURE_MAX_AGE_S", raising=False)
    monkeypatch.setattr(config, "SCALP_POOL_CAPITAL_SHARE_PCT", 50.0)


@pytest.fixture()
def db(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    monkeypatch.setattr(ledger, "sync_peer_pnl", lambda d: None)
    monkeypatch.setattr(shared_exposure, "publish_own_exposure", lambda d, v: None)
    monkeypatch.setattr(dhan_client, "get_funds", lambda d: {"availabelBalance": 60_000.0})
    yield s
    s.close()


def _publish(db, value=40_000.0, age_s=0.0, naive=False):
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    db.add(models.SharedServiceExposure(service_name="real-trade-service", open_positions_market_value=value,
                                        updated_at=ts.replace(tzinfo=None) if naive else ts))
    db.commit()


def _total(db):
    ledger.sync_from_broker(db)
    return ledger._get_or_create(db).total_allocated_capital


def test_a_fresh_figure_is_credited(db):
    _publish(db, age_s=60)
    assert _total(db) == pytest.approx(50_000.0)


def test_a_stale_figure_is_not_credited(db):
    _publish(db, age_s=1200)
    assert _total(db) == pytest.approx(30_000.0)


def test_an_age_exactly_at_the_limit_is_still_credited(db, monkeypatch):
    monkeypatch.setenv("SCALP_POOL_PEER_EXPOSURE_MAX_AGE_S", "100")
    _publish(db, age_s=1)
    monkeypatch.setattr(shared_exposure, "get_other_service_exposure_age", lambda d: 100.0)
    assert _total(db) == pytest.approx(50_000.0)
    monkeypatch.setattr(shared_exposure, "get_other_service_exposure_age", lambda d: 100.5)
    assert _total(db) == pytest.approx(30_000.0)


def test_the_limit_is_configurable(db, monkeypatch):
    monkeypatch.setenv("SCALP_POOL_PEER_EXPOSURE_MAX_AGE_S", "30")
    _publish(db, age_s=60)
    assert _total(db) == pytest.approx(30_000.0)


def test_zero_switches_the_stale_check_off(db, monkeypatch):
    monkeypatch.setenv("SCALP_POOL_PEER_EXPOSURE_MAX_AGE_S", "0")
    _publish(db, age_s=99_999)
    assert _total(db) == pytest.approx(50_000.0)


@pytest.mark.parametrize("raw, want", [("", 900.0), ("  ", 900.0), ("abc", 900.0), ("-5", 900.0), ("120", 120.0),
                                       ("0", 0.0)])
def test_max_age_parsing_is_blank_safe(monkeypatch, raw, want):
    monkeypatch.setenv("SCALP_POOL_PEER_EXPOSURE_MAX_AGE_S", raw)
    assert ledger._peer_exposure_max_age_s() == want


def test_a_naive_timestamp_is_read_as_utc(db):
    _publish(db, age_s=1200, naive=True)
    assert _total(db) == pytest.approx(30_000.0)
    assert shared_exposure.get_other_service_exposure_age(db) == pytest.approx(1200, abs=5)


def test_an_unreadable_age_keeps_the_credit(db, monkeypatch):
    _publish(db, age_s=5000)
    monkeypatch.setattr(shared_exposure, "get_other_service_exposure_age", lambda d: None)
    assert _total(db) == pytest.approx(50_000.0)


def test_no_row_gives_no_age_and_no_credit(db):
    assert shared_exposure.get_other_service_exposure_age(db) is None
    assert _total(db) == pytest.approx(30_000.0)


def test_age_read_never_raises():
    assert shared_exposure.get_other_service_exposure_age(object()) is None


def test_the_credit_switch_still_wins_over_a_fresh_figure(db, monkeypatch):
    monkeypatch.setenv("SCALP_POOL_CREDIT_PEER_EXPOSURE", "0")
    _publish(db, age_s=1)
    assert _total(db) == pytest.approx(30_000.0)


def test_a_stale_figure_logs_one_warning_per_bucket(db, caplog):
    import logging
    _publish(db, age_s=1200)
    with caplog.at_level(logging.WARNING, logger=ledger.logger.name):
        ledger.sync_from_broker(db)
        ledger.sync_from_broker(db)
    assert sum("not credited" in r.getMessage() for r in caplog.records) == 1


def test_a_refreshed_figure_is_credited_again(db):
    _publish(db, age_s=1200)
    assert _total(db) == pytest.approx(30_000.0)
    row = db.query(models.SharedServiceExposure).one()
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    assert _total(db) == pytest.approx(50_000.0)
