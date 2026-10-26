"""
E214 - exit_engine.exit.evaluate_mode: a profit-TARGET partial sale could fire on an old price (the priority lane serves a last-good tick up to
90 s old, source "stale_last_good(...)"). Now the partial waits for a tick at most EXIT_TARGET_MAX_TICK_AGE_S old; stops and emergency exits
still act on any tick. EXIT_TARGET_STALE_GUARD_ENABLED=false restores the old behaviour.

Run from services/real-trade-service:
    python3 -m pytest tests/test_e214_stale_tick_no_target_partial.py -q
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

import config
import models
import exit_engine.exit as ex
from market_feed.feed import Tick

NOW = datetime(2026, 10, 26, 5, 0, tzinfo=timezone.utc)


def _tick(age_s=1.0, source="test", age_known=True, price=111.0):
    return Tick(symbol="T", price=price, as_of=NOW - timedelta(seconds=age_s), atr=None, source=source, age_known=age_known)


class TestHelper:
    def test_fresh_tick_passes(self):
        assert ex._stale_tick_blocks_target_sale(_tick(5), NOW) is None

    def test_old_tick_blocks(self):
        assert "old" in ex._stale_tick_blocks_target_sale(_tick(45), NOW)

    def test_served_last_good_blocks_even_if_young(self):
        assert ex._stale_tick_blocks_target_sale(_tick(2, source="stale_last_good(bulk)"), NOW)

    def test_unknown_age_is_not_blocked_by_age(self):
        assert ex._stale_tick_blocks_target_sale(_tick(500, age_known=False), NOW) is None

    def test_switch_off(self, monkeypatch):
        monkeypatch.setattr(config, "EXIT_TARGET_STALE_GUARD_ENABLED", False)
        assert ex._stale_tick_blocks_target_sale(_tick(500), NOW) is None


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _pos(db, entry=100.0, stop=95.0, target=110.0):
    p = models.TradePosition(mode="REAL", symbol="T", status="OPEN", qty_open=10, avg_entry_price=entry, current_stop=stop, current_target=target,
                             initial_stop_distance=entry - stop, opened_at=datetime.now(timezone.utc) - timedelta(days=1))
    db.add(p); db.commit(); db.refresh(p)
    return p


def _feed(monkeypatch, **kw):
    async def _q(symbols, **_k):
        t = Tick(symbol="T", price=kw["price"], as_of=datetime.now(timezone.utc) - timedelta(seconds=kw.get("age", 0)), atr=None, source=kw.get("source", "test"))
        return {"T": t}
    monkeypatch.setattr(ex, "get_quotes", _q)


def _sells(monkeypatch):
    sent = []
    monkeypatch.setattr(ex, "_send_real_sell", lambda db, pos, qty, reason, *a, **k: sent.append(reason) or True)
    return sent


class TestCycle:
    def test_stale_target_tick_sends_no_partial(self, db, monkeypatch):
        _pos(db); _feed(monkeypatch, price=111.0, age=70, source="stale_last_good(bulk)"); sent = _sells(monkeypatch)
        asyncio.run(ex.evaluate_mode(db, "REAL"))
        assert sent == []

    def test_fresh_target_tick_still_sells(self, db, monkeypatch):
        _pos(db); _feed(monkeypatch, price=111.0, age=1); sent = _sells(monkeypatch)
        asyncio.run(ex.evaluate_mode(db, "REAL"))
        assert sent == ["target_hit_partial"]

    def test_stale_stop_tick_still_stops(self, db, monkeypatch):
        _pos(db); _feed(monkeypatch, price=94.0, age=70, source="stale_last_good(bulk)"); sent = _sells(monkeypatch)
        asyncio.run(ex.evaluate_mode(db, "REAL"))
        assert sent == ["stop_hit"]
