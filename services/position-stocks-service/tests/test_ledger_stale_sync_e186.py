"""E186: new entries are refused while the scalp pool's last successful Dhan sync is older than _MAX_SYNC_AGE_S."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.test_ledger_coverage import env, _seed_ledger  # noqa: F401
from capital import ledger


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(ledger, "_sync_log_state", {})


def _synced(db, age_s):
    row = ledger._get_or_create(db)
    row.last_synced_from_broker_at = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    db.commit()


def test_recent_sync_allows_the_entry(env):
    db, _ = env
    _seed_ledger(db)
    _synced(db, 30)
    assert ledger.reserve_capital(db, adaptive_stop_pct=2.0) is not None


def test_stale_sync_refuses_and_leaves_the_pool_untouched(env, caplog):
    db, _ = env
    _seed_ledger(db, total=100_000.0, avail=90_000.0)
    _synced(db, ledger._MAX_SYNC_AGE_S + 60)
    assert ledger.reserve_capital(db, adaptive_stop_pct=2.0) is None
    assert ledger._get_or_create(db).available_capital == pytest.approx(90_000.0)
    assert "last synced from Dhan" in caplog.text


def test_naive_timestamp_from_sqlite_is_read_as_utc(env):
    db, _ = env
    _seed_ledger(db)
    row = ledger._get_or_create(db)
    row.last_synced_from_broker_at = (datetime.now(timezone.utc) - timedelta(seconds=ledger._MAX_SYNC_AGE_S + 60)).replace(tzinfo=None)
    db.commit()
    assert ledger.reserve_capital(db, adaptive_stop_pct=2.0) is None


def test_a_ledger_that_never_recorded_a_sync_is_not_blocked(env):
    db, _ = env
    _seed_ledger(db)
    assert ledger._get_or_create(db).last_synced_from_broker_at is None
    assert ledger.reserve_capital(db, adaptive_stop_pct=2.0) is not None


def test_a_successful_sync_clears_the_block(env, monkeypatch):
    from execution import dhan_client
    db, _ = env
    _seed_ledger(db)
    _synced(db, ledger._MAX_SYNC_AGE_S + 600)
    assert ledger.reserve_capital(db, adaptive_stop_pct=2.0) is None
    monkeypatch.setattr(dhan_client, "get_funds", lambda db_: {"availabelBalance": 50_000.0})
    ledger.sync_from_broker(db)
    assert ledger.reserve_capital(db, adaptive_stop_pct=2.0) is not None
