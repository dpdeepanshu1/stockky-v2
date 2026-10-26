"""E175: a structure exit (STRUCTURE_EXIT_MODE=exit) sells the position, so the cycle tally must count it in full_exits.
Before E175 the sell happened but neither DEMO nor REAL added to any counter. Run from services/real-trade-service:
    python3 -m pytest tests/test_e175_structure_exit_tally.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import exit_engine.exit as ex
from exit_engine import structure_exit as sx
from entry_engine import structure_gate as sgate
from market_feed.feed import Tick


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    s.add(models.TradeAccount(mode="DEMO", starting_capital=100000.0, current_equity=100000.0, cash_available=100000.0))
    s.commit()
    yield s
    s.close()


def _position(db, mode):
    pos = models.TradePosition(
        mode=mode, symbol="TESTCO", status="OPEN", qty_open=10, avg_entry_price=100.0,
        current_stop=95.0, current_target=150.0, initial_stop_distance=5.0,
        opened_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)
    return pos


def _wire(monkeypatch, sent):
    t = Tick(symbol="TESTCO", price=105.0, as_of=datetime.now(timezone.utc), atr=None, source="test")

    async def _q(symbols, **_kw):
        return {"TESTCO": t}

    monkeypatch.setattr(ex, "get_quotes", _q)
    monkeypatch.setattr(sx, "mode", lambda: "exit")
    monkeypatch.setattr(sgate, "peek_summary", lambda sym: {"any": "summary"})
    monkeypatch.setattr(sx, "evaluate", lambda *a, **k: {"action": "exit", "reasons": ["test"]})
    monkeypatch.setattr(sx, "record", lambda *a, **k: None)

    def _fake_sell(db, position, qty, reason, **kw):
        sent.append(reason)
        return True

    monkeypatch.setattr(ex, "_send_real_sell", _fake_sell)


def test_demo_structure_exit_counted(db, monkeypatch):
    pos = _position(db, "DEMO")
    _wire(monkeypatch, [])
    tally = asyncio.run(ex.evaluate_mode(db, "DEMO"))
    db.refresh(pos)
    assert pos.status != "OPEN"
    assert tally["full_exits"] == 1 and tally["held"] == 0


def test_real_structure_exit_counted(db, monkeypatch):
    _position(db, "REAL")
    sent = []
    _wire(monkeypatch, sent)
    monkeypatch.setattr(ex, "_has_pending_real_sell", lambda db, sym: False)

    async def _noop(db, mode):
        return 0

    monkeypatch.setattr(ex, "expire_stale_exit_orders", _noop)
    try:
        from execution import broker_stop

        async def _sync(*a, **k):
            return None

        monkeypatch.setattr(broker_stop, "sync_stops", _sync)
    except Exception:
        pass
    tally = asyncio.run(ex.evaluate_mode(db, "REAL"))
    assert sent == ["structure_exit"]
    assert tally["full_exits"] == 1 and tally["held"] == 0
