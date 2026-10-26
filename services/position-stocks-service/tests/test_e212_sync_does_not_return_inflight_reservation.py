"""E212: a manual POST /ledger/sync (sync_from_broker) landing between reserve_capital and the position row being written must not hand the
reservation back to the pool through the "no open positions -> available = total" hard reset."""
from __future__ import annotations

import time

import pytest

import config
from capital import ledger
from execution import dhan_client
from tests.test_ledger_coverage import env, _seed_ledger  # noqa: F401


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(ledger, "_sync_log_state", {})
    # a balance whose scalp-pool share equals the seeded 100_000 total, so a sync does not change the pool size
    bal = 100_000.0 * 100.0 / float(config.SCALP_POOL_CAPITAL_SHARE_PCT)
    monkeypatch.setattr(dhan_client, "get_funds", lambda db_: {"availabelBalance": bal})


def _avail(db):
    db.expire_all()
    return ledger._get_or_create(db).available_capital


def test_sync_right_after_a_reservation_keeps_it_reserved(env):
    db, _ = env
    _seed_ledger(db, total=100_000.0, avail=100_000.0)
    pv = ledger.reserve_capital(db, adaptive_stop_pct=2.0)
    assert pv is not None
    after_reserve = _avail(db)
    ledger.sync_from_broker(db)                       # no position row yet: open_count == 0
    assert _avail(db) == pytest.approx(after_reserve)
    assert _avail(db) < ledger._get_or_create(db).total_allocated_capital


def test_reserve_additional_also_counts(env):
    db, _ = env
    _seed_ledger(db, total=100_000.0, avail=100_000.0)
    assert ledger.reserve_additional(db, 1_000.0) is True
    ledger.sync_from_broker(db)
    assert _avail(db) < ledger._get_or_create(db).total_allocated_capital


def test_after_the_grace_the_normal_reset_applies(env, monkeypatch):
    db, _ = env
    _seed_ledger(db, total=100_000.0, avail=100_000.0)
    ledger.reserve_capital(db, adaptive_stop_pct=2.0)
    monkeypatch.setattr(ledger, "_last_reserve_mono", time.monotonic() - ledger._RESERVE_GRACE_S - 5)
    ledger.sync_from_broker(db)
    assert _avail(db) == pytest.approx(ledger._get_or_create(db).total_allocated_capital)


def test_no_reservation_means_the_normal_reset(env):
    db, _ = env
    _seed_ledger(db, total=100_000.0, avail=10_000.0)
    ledger.sync_from_broker(db)
    assert _avail(db) == pytest.approx(ledger._get_or_create(db).total_allocated_capital)


def test_a_lower_balance_still_clamps_available_down(env, monkeypatch):
    db, _ = env
    _seed_ledger(db, total=100_000.0, avail=100_000.0)
    ledger.reserve_capital(db, adaptive_stop_pct=2.0)
    monkeypatch.setattr(dhan_client, "get_funds", lambda db_: {"availabelBalance": 20_000.0})
    ledger.sync_from_broker(db)
    row = ledger._get_or_create(db)
    assert row.available_capital <= row.total_allocated_capital
