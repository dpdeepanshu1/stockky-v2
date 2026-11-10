"""
E338 - real-trade-service's symbol lock (execution/shared_symbol_lock.try_claim).

When our INSERT collided (IntegrityError) and the re-read found NO row (the other holder released in between, or the collision was not the unique
key), try_claim returned True although this service held nothing: the BUY went out with no lock and position-stocks-service could buy the same
symbol. The INSERT is now tried once more; two collisions with still no row = fail closed (False). The other branches are unchanged.

Run from services/real-trade-service:   python3 -m pytest tests/test_e338_symbol_lock_vanished_row.py -q
"""
from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy.exc import IntegrityError

from execution import shared_symbol_lock as ssl


def _collide():
    return IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))


def _quiet_alerts():
    sent = []
    sys.modules["notifier"] = types.SimpleNamespace(notify_fire_and_forget=lambda text: sent.append(text))
    ssl._last_alert["t"] = None
    return sent


def _row(service):
    return types.SimpleNamespace(held_by_service=service, held_by_mode="REAL", claimed_at=None)


def test_collision_then_row_gone_then_second_insert_works_returns_true_with_a_row_held():
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.side_effect = [None, None, None]
    db.commit.side_effect = [_collide(), None]
    assert ssl.try_claim(db, "abc") is True
    assert db.add.call_count == 2 and db.commit.call_count == 2
    db.rollback.assert_called_once()


def test_collision_with_no_row_twice_is_refused_not_granted():
    sent = _quiet_alerts()
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.side_effect = [None, None, None, None]
    db.commit.side_effect = _collide()
    assert ssl.try_claim(db, "abc") is False
    assert db.rollback.call_count == 2
    assert len(sent) == 1 and "New BUY entries are being refused" in sent[0]


def test_collision_then_peer_row_is_still_a_lost_race():
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.side_effect = [None, _row("position-stocks-service")]
    db.commit.side_effect = _collide()
    assert ssl.try_claim(db, "abc") is False
    assert db.add.call_count == 1


def test_collision_then_our_own_row_is_still_allowed():
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.side_effect = [None, _row("real-trade-service")]
    db.commit.side_effect = _collide()
    assert ssl.try_claim(db, "abc") is True


def test_plain_claim_and_peer_held_are_unchanged():
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    assert ssl.try_claim(db, "abc", "DEMO") is True
    assert db.add.call_count == 1 and db.commit.call_count == 1
    assert db.add.call_args[0][0].held_by_mode == "DEMO"
    db2 = MagicMock()
    db2.query.return_value.filter_by.return_value.first.return_value = _row("position-stocks-service")
    assert ssl.try_claim(db2, "abc") is False
    db2.add.assert_not_called()


def test_a_generic_error_still_fails_closed_and_rolls_back():
    _quiet_alerts()
    db = MagicMock()
    db.query.side_effect = RuntimeError("db down")
    assert ssl.try_claim(db, "abc") is False
    db.rollback.assert_called_once()
