"""group 397 (DB-backed): the exit loop records each open position's peak / trough, the columns exist and are migrated, and the records route data carries them.
Reuses the evaluate_mode harness (DEMO mode, in-memory SQLite)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine, inspect, text

import db as dbmod
import exit_engine.exit as ex
import models
import trade_records as tr
from market_feed.feed import Tick
from tests.test_exit_evaluate_mode import db, make_position, quotes, run, tick  # noqa: F401  (fixtures / helpers)


class TestExitLoopRecords:
    def test_a_rise_then_a_dip_keeps_both_extremes(self, db, monkeypatch):
        pos = make_position(db, entry=100.0, stop=95.0, target=120.0)
        for px in (103.0, 99.0, 101.0):
            quotes(monkeypatch, {"TESTCO": tick(px)})
            run(ex.evaluate_mode(db, "DEMO"))
        db.refresh(pos)
        assert pos.max_price_seen == 103.0 and pos.min_price_seen == 99.0

    def test_the_price_that_triggers_the_stop_is_recorded(self, db, monkeypatch):
        pos = make_position(db, entry=100.0, stop=95.0, target=110.0)
        quotes(monkeypatch, {"TESTCO": tick(94.0)})
        run(ex.evaluate_mode(db, "DEMO"))
        db.refresh(pos)
        assert pos.status == "CLOSED" and pos.min_price_seen == 94.0 and pos.max_price_seen == 100.0

    def test_a_stale_tick_is_not_recorded(self, db, monkeypatch):
        pos = make_position(db, entry=100.0, stop=95.0, target=120.0)
        old = Tick(symbol="TESTCO", price=108.0, as_of=datetime.now(timezone.utc) - timedelta(minutes=30), atr=None, source="test")
        quotes(monkeypatch, {"TESTCO": old})
        run(ex.evaluate_mode(db, "DEMO"))
        db.refresh(pos)
        assert pos.max_price_seen is None

    def test_a_recorder_failure_never_stops_the_exit_check(self, db, monkeypatch):
        pos = make_position(db, entry=100.0, stop=95.0, target=110.0)
        monkeypatch.setattr(ex._excursion, "next_extremes", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        quotes(monkeypatch, {"TESTCO": tick(94.0)})
        assert run(ex.evaluate_mode(db, "DEMO"))["full_exits"] == 1
        db.refresh(pos)
        assert pos.status == "CLOSED"

    def test_the_recorded_values_reach_the_trade_record(self, db, monkeypatch):
        pos = make_position(db, entry=100.0, stop=98.0, target=130.0)
        for px in (101.5, 97.9):                       # up 1.5%, then through the 2% stop
            quotes(monkeypatch, {"TESTCO": tick(px)})
            run(ex.evaluate_mode(db, "DEMO"))
        db.refresh(pos)
        recs = tr.build_records([pos], [], [], [], [], [], [])
        assert recs and recs[0]["max_gain_pct"] == 1.5 and recs[0]["max_drawdown_pct"] == -2.1


class TestColumns:
    def test_model_columns_are_nullable_floats(self):
        t = models.TradePosition.__table__.c
        assert t.max_price_seen.nullable and t.min_price_seen.nullable

    def test_migration_adds_both_to_a_table_that_predates_them_and_is_idempotent(self):
        eng = create_engine("sqlite:///:memory:")
        with eng.begin() as c:
            c.execute(text("CREATE TABLE trade_positions (id INTEGER PRIMARY KEY)"))
            c.execute(text("CREATE TABLE trade_decisions (id INTEGER PRIMARY KEY)"))
            c.execute(text("CREATE TABLE trade_risk_config (id INTEGER PRIMARY KEY)"))
        dbmod._ensure_cost_model_columns(eng, "postgres")
        cols = {c["name"] for c in inspect(eng).get_columns("trade_positions")}
        assert {"max_price_seen", "min_price_seen"} <= cols
        dbmod._ensure_cost_model_columns(eng, "postgres")
