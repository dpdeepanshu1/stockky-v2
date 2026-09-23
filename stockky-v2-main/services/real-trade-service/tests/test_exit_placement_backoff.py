"""Session 72 (#4): a SELL that fails at PLACEMENT (SDK/API exception) must feed the same
consecutive_exit_failures backoff as a dead order — previously it retried every exit cycle.
Run from services/real-trade-service:  python -m pytest tests/test_exit_placement_backoff.py -q"""
import asyncio, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from execution import dhan_client
import exit_engine.exit as ex

_engine = create_engine("sqlite:///:memory:")


@pytest.fixture()
def env(monkeypatch):
    models.Base.metadata.drop_all(_engine); models.Base.metadata.create_all(_engine)
    db = sessionmaker(bind=_engine)()
    pos = models.TradePosition(mode="REAL", symbol="DATAMATICS", status="OPEN", qty_open=10, avg_entry_price=100.0,
                               opened_at=datetime.now(timezone.utc), broker_imported=True)
    db.add(pos); db.commit()
    calls = {"n": 0, "err": "Dhan API error: Validate Qty from CDSL"}
    monkeypatch.setattr(dhan_client, "get_security_id", lambda db_, sym: "1234")

    def _boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError(calls["err"])
    monkeypatch.setattr(dhan_client, "place_order", _boom)
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: None)
    return db, pos, calls


def _sell(db, pos):
    return asyncio.run(ex._send_real_sell(db, pos, 10, "emergency_gap_down")) if asyncio.iscoroutinefunction(ex._send_real_sell) \
        else ex._send_real_sell(db, pos, 10, "emergency_gap_down")


def test_persistent_rejection_backs_off_instead_of_retrying_every_cycle(env):
    db, pos, calls = env
    assert _sell(db, pos) is False and calls["n"] == 1
    assert pos.consecutive_exit_failures == 1                  # placement failure now counted
    for _ in range(5):                                         # the next 5 exit cycles: inside the cooldown
        assert _sell(db, pos) is False
    assert calls["n"] == 1, "SELL was re-sent during the cooldown (retry storm)"


def test_oversell_is_not_backed_off(env, monkeypatch):
    db, pos, calls = env
    calls["err"] = "Dhan API error: you cannot sell more than the quantity you currently hold"
    monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: [])
    _sell(db, pos)
    assert (pos.consecutive_exit_failures or 0) == 0           # oversell wants a fast retry after the qty sync


def test_generic_error_backs_off_only_after_the_escalation_streak(env):
    db, pos, calls = env
    calls["err"] = "some transient network hiccup"
    for i in range(ex.EXIT_REJECT_STREAK_ESCALATE_AT - 1):
        _sell(db, pos)
    assert (pos.consecutive_exit_failures or 0) == 0           # a blip still retries next cycle
    _sell(db, pos)
    assert (pos.consecutive_exit_failures or 0) >= 1           # ...but a sustained failure backs off
