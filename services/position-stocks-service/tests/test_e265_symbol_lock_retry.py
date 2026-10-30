"""E265 (2026-10-30): "position-stocks-service: the symbol lock check could not reach the database (DatabaseError)".

One transient driver error (a DatabaseError from a slow / reconnecting Oracle, a pool timeout) used to refuse the BUY at once (fail-closed, group 448).
Now capital/shared_symbol_lock.try_claim retries a database-driver error SYMBOL_LOCK_DB_RETRIES times (default 1) after a rollback and a short pause,
and only then fails closed. A "table does not exist" error also re-creates the table. The Telegram alert now carries the driver's own message.
Programming errors (not a driver error) are never retried. SYMBOL_LOCK_DB_RETRIES=0 = the old behaviour.

Run from services/position-stocks-service:  python3 -m pytest tests/test_e265_symbol_lock_retry.py -q
"""
from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from sqlalchemy.exc import DatabaseError, OperationalError

from capital import shared_symbol_lock as sl


def _db_error(msg="ORA-03113: end-of-file on communication channel"):
    return DatabaseError("SELECT 1", {}, Exception(msg))


def _alerts(monkeypatch):
    sent = []
    monkeypatch.setitem(sys.modules, "notifier", types.SimpleNamespace(notify_fire_and_forget=lambda text: sent.append(text)))
    monkeypatch.setitem(sl._last_alert, "t", None)
    return sent


def _no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(sl.time, "sleep", lambda s: slept.append(s))
    return slept


def _free_symbol_db(first_errors):
    """Session double: the lookup raises each error in `first_errors` in turn, then finds no row (the claim then inserts and commits)."""
    db = MagicMock()
    q = db.query.return_value.filter_by.return_value.first
    q.side_effect = list(first_errors) + [None] * 5
    return db


def test_one_database_error_is_retried_and_the_claim_succeeds(monkeypatch):
    sent = _alerts(monkeypatch)
    slept = _no_sleep(monkeypatch)
    db = _free_symbol_db([_db_error()])
    assert sl.try_claim(db, "abc") is True
    db.rollback.assert_called_once()          # the failed attempt was rolled back before the retry
    db.add.assert_called_once()               # the retry inserted the claim
    assert sent == []                          # no "could not reach the database" alert for a blip
    assert slept == [config.SYMBOL_LOCK_DB_RETRY_DELAY_S]


def test_operational_error_is_retried_too(monkeypatch):
    _alerts(monkeypatch)
    _no_sleep(monkeypatch)
    db = _free_symbol_db([OperationalError("SELECT 1", {}, Exception("DPY-4011: the database or network closed the connection"))])
    assert sl.try_claim(db, "ABC") is True


def test_a_second_database_error_still_fails_closed_and_alerts_with_the_real_message(monkeypatch):
    sent = _alerts(monkeypatch)
    _no_sleep(monkeypatch)
    db = _free_symbol_db([_db_error("ORA-00942: table or view does not exist"), _db_error("ORA-00942: table or view does not exist")])
    assert sl.try_claim(db, "ABC") is False
    assert len(sent) == 1
    assert "DatabaseError" in sent[0] and "ORA-00942" in sent[0]
    assert "New BUY entries are being refused" in sent[0]


def test_retries_zero_is_the_old_behaviour(monkeypatch):
    sent = _alerts(monkeypatch)
    slept = _no_sleep(monkeypatch)
    monkeypatch.setattr(config, "SYMBOL_LOCK_DB_RETRIES", 0, raising=False)
    db = _free_symbol_db([_db_error()])
    assert sl.try_claim(db, "ABC") is False
    assert slept == [] and len(sent) == 1
    db.rollback.assert_called_once()


def test_a_programming_error_is_not_retried(monkeypatch):
    _alerts(monkeypatch)
    slept = _no_sleep(monkeypatch)
    db = MagicMock()
    db.query.side_effect = RuntimeError("bug")
    assert sl.try_claim(db, "ABC") is False
    assert slept == []
    assert db.query.call_count == 1


def test_fail_open_switch_still_allows_after_the_retry_fails(monkeypatch):
    sent = _alerts(monkeypatch)
    _no_sleep(monkeypatch)
    monkeypatch.setattr(config, "SHARED_GATES_FAIL_CLOSED", False, raising=False)
    db = _free_symbol_db([_db_error(), _db_error()])
    assert sl.try_claim(db, "ABC") is True and sent == []


def test_a_missing_table_is_recreated_before_the_retry(monkeypatch):
    _alerts(monkeypatch)
    _no_sleep(monkeypatch)
    monkeypatch.setitem(sl._last_ensure, "t", None)
    created = []
    monkeypatch.setattr(sl.SharedSymbolLock.__table__, "create", lambda bind=None, checkfirst=False: created.append((bind, checkfirst)))
    db = _free_symbol_db([_db_error("ORA-00942: table or view does not exist")])
    db.get_bind.return_value = "ENGINE"
    assert sl.try_claim(db, "ABC") is True
    assert created == [("ENGINE", True)]


def test_table_recreate_is_rate_limited(monkeypatch):
    _alerts(monkeypatch)
    _no_sleep(monkeypatch)
    monkeypatch.setitem(sl._last_ensure, "t", None)
    created = []
    monkeypatch.setattr(sl.SharedSymbolLock.__table__, "create", lambda bind=None, checkfirst=False: created.append(1))
    for _ in range(2):
        db = _free_symbol_db([_db_error("ORA-00942: table or view does not exist")])
        sl.try_claim(db, "ABC")
    assert len(created) == 1


def test_a_failing_table_recreate_never_breaks_the_gate(monkeypatch):
    _alerts(monkeypatch)
    _no_sleep(monkeypatch)
    monkeypatch.setitem(sl._last_ensure, "t", None)

    def boom(bind=None, checkfirst=False):
        raise RuntimeError("no DDL rights")
    monkeypatch.setattr(sl.SharedSymbolLock.__table__, "create", boom)
    db = _free_symbol_db([_db_error("ORA-00942: x"), _db_error("ORA-00942: x")])
    assert sl.try_claim(db, "ABC") is False


def test_peer_lock_is_still_blocked_without_any_retry(monkeypatch):
    slept = _no_sleep(monkeypatch)
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = types.SimpleNamespace(
        held_by_service="real-trade-service", held_by_mode="REAL", claimed_at=None)
    assert sl.try_claim(db, "ABC") is False
    assert slept == []


def test_err_brief_uses_the_driver_message_first_line():
    e = _db_error("ORA-01017: invalid credential\nsecond line")
    assert sl._err_brief(e).startswith("ORA-01017")
    assert "\n" not in sl._err_brief(e)
    assert sl._err_brief(Exception("")) == "no message"


def test_config_defaults():
    assert config.SYMBOL_LOCK_DB_RETRIES == 1
    assert config.SYMBOL_LOCK_DB_RETRY_DELAY_S == 0.3
