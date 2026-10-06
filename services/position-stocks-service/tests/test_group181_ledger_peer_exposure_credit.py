"""group 181: the scalp pool's baseline credits its share of real-trade-service's open positions, so a real-trade
buy no longer shrinks the pool; capped at free cash + own committed capital; switchable; fail-open."""
import logging
from datetime import datetime, timezone

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
    monkeypatch.setattr(config, "SCALP_POOL_CAPITAL_SHARE_PCT", 50.0)


@pytest.fixture()
def db(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    monkeypatch.setattr(ledger, "sync_peer_pnl", lambda d: None)
    monkeypatch.setattr(shared_exposure, "publish_own_exposure", lambda d, v: None)
    yield s
    s.close()


def _funds(monkeypatch, balance):
    monkeypatch.setattr(dhan_client, "get_funds", lambda d: {"availabelBalance": balance})


def _peer(monkeypatch, value):
    monkeypatch.setattr(shared_exposure, "get_other_service_exposure", lambda d: value)


def _add_position(db, capital_risked):
    db.add(models.ScalpPosition(
        symbol="ABC", window_source="5m", adaptive_target_pct=2.0, adaptive_stop_pct=1.0, status="OPEN",
        quantity=10, entry_price=500.0, capital_risked=capital_risked, overnight_converted_to_cnc=False,
        dhan_security_id="9", target_price=510.0, stop_price=490.0, opened_at=datetime.now(timezone.utc)))
    db.commit()


def test_peer_positions_are_credited_back(db, monkeypatch):
    _funds(monkeypatch, 60_000.0)       # free cash after real-trade bought 40k of a 100k account
    _peer(monkeypatch, 40_000.0)
    ledger.sync_from_broker(db)
    row = ledger._get_or_create(db)
    assert row.total_allocated_capital == pytest.approx(50_000.0)   # half of the 100k account, not half of 60k
    assert row.available_capital == pytest.approx(50_000.0)


def test_no_peer_exposure_is_unchanged(db, monkeypatch):
    _funds(monkeypatch, 60_000.0)
    _peer(monkeypatch, 0.0)
    ledger.sync_from_broker(db)
    assert ledger._get_or_create(db).total_allocated_capital == pytest.approx(30_000.0)


def test_credit_never_sizes_the_pool_above_free_cash(db, monkeypatch):
    _funds(monkeypatch, 60_000.0)
    _peer(monkeypatch, 200_000.0)       # would credit 100k
    ledger.sync_from_broker(db)
    assert ledger._get_or_create(db).total_allocated_capital == pytest.approx(60_000.0)


def test_own_committed_capital_is_part_of_the_ceiling(db, monkeypatch):
    _funds(monkeypatch, 60_000.0)
    _add_position(db, 10_000.0)
    _peer(monkeypatch, 200_000.0)
    ledger.sync_from_broker(db)
    assert ledger._get_or_create(db).total_allocated_capital == pytest.approx(70_000.0)   # 60k cash + 10k committed


def test_credit_adds_to_own_committed_capital_below_the_ceiling(db, monkeypatch):
    _funds(monkeypatch, 60_000.0)
    _add_position(db, 10_000.0)
    _peer(monkeypatch, 20_000.0)
    ledger.sync_from_broker(db)
    assert ledger._get_or_create(db).total_allocated_capital == pytest.approx(30_000.0 + 10_000.0 + 10_000.0)


@pytest.mark.parametrize("raw", ["0", "false", "off", " No "])
def test_switch_off_restores_the_old_figure(db, monkeypatch, raw):
    monkeypatch.setenv("SCALP_POOL_CREDIT_PEER_EXPOSURE", raw)
    _funds(monkeypatch, 60_000.0)
    _peer(monkeypatch, 40_000.0)
    ledger.sync_from_broker(db)
    assert ledger._get_or_create(db).total_allocated_capital == pytest.approx(30_000.0)


def test_unreadable_exposure_fails_open(db, monkeypatch):
    _funds(monkeypatch, 60_000.0)

    def boom(d):
        raise RuntimeError("db down")
    monkeypatch.setattr(shared_exposure, "get_other_service_exposure", boom)
    ledger.sync_from_broker(db)
    assert ledger._get_or_create(db).total_allocated_capital == pytest.approx(30_000.0)


def test_reads_the_real_shared_table(db, monkeypatch):
    db.add(models.SharedServiceExposure(service_name="real-trade-service", open_positions_market_value=40_000.0))
    db.commit()
    _funds(monkeypatch, 60_000.0)
    ledger.sync_from_broker(db)
    assert ledger._get_or_create(db).total_allocated_capital == pytest.approx(50_000.0)


def test_a_real_trade_buy_no_longer_shrinks_the_pool(db, monkeypatch):
    """Same account value before and after the peer's buy gives the same pool."""
    _funds(monkeypatch, 100_000.0)
    _peer(monkeypatch, 0.0)
    ledger.sync_from_broker(db)
    before = ledger._get_or_create(db).total_allocated_capital
    _funds(monkeypatch, 60_000.0)       # real-trade spent 40k of free cash
    _peer(monkeypatch, 40_000.0)        # ... and now publishes 40k of open positions
    ledger.sync_from_broker(db)
    assert ledger._get_or_create(db).total_allocated_capital == pytest.approx(before)


def test_credit_is_logged_once_per_change(db, monkeypatch, caplog):
    _funds(monkeypatch, 60_000.0)
    _peer(monkeypatch, 40_000.0)
    with caplog.at_level(logging.INFO):
        ledger.sync_from_broker(db)
        ledger.sync_from_broker(db)
    msgs = [r.getMessage() for r in caplog.records if "credited" in r.getMessage()]
    assert len(msgs) == 1 and "20000.00" in msgs[0]
