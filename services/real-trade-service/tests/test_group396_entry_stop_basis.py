"""group 396: the entry decision records where its initial stop came from (entry_engine/entry.py -> trade_decisions.stop_basis), the column
exists on the model and is migrated onto an old table. DEMO mode, same harness as test_group351_structure_entry_wiring.py."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine, inspect, text

import config
import db as dbmod
import models
from entry_engine import entry, structure_gate
from tests.test_entry_evaluate_mode import approve_all, db, make_candidate, pin, quotes, run, tick  # noqa: F401  (fixtures)


def _sum(**kw):
    s = {"ms_quality": "ok", "ms_ver": "1.0.0", "ms_asof": datetime.now(timezone.utc).isoformat(), "ms_trend": "bullish",
         "ms_price": 100.0, "ms_atr": 2.0, "ms_chase_atr": 0.2, "ms_stretch_atr": 1.0, "ms_room_atr": 3.0, "ms_stop": 96.0,
         "ms_target": 108.0, "ms_rr_flags": []}
    s.update(kw)
    return s


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    for k, v in (("STRUCTURE_GATE_MODE", "off"), ("STRUCTURE_GATE_DEMO_MODE", "off"), ("STRUCTURE_REQUIRE_DATA", False),
                 ("STRUCTURE_STOP_MODE", "off"), ("STRUCTURE_MAX_CHASE_ATR", 1.5), ("STRUCTURE_MAX_STRETCH_ATR", 3.0),
                 ("STRUCTURE_MIN_ROOM_ATR", 0.5), ("STRUCTURE_MIN_RR", 1.2), ("STRUCTURE_BEARISH_BLOCK", True),
                 ("STRUCTURE_REJECT_STOP_WIDE", True), ("STRUCTURE_MAX_AGE_DAYS", 7.0), ("STRUCTURE_MAX_PRICE_DEV_PCT", 15.0),
                 ("STRUCTURE_BEARISH_BOS_BARS", 5), ("STRUCTURE_STOP_MAX_RISK_MULT", 1.6), ("STRUCTURE_STOP_MIN_RISK_MULT", 0.5)):
        monkeypatch.setattr(config, k, v, raising=False)
    structure_gate.reset()


def _dec(db, symbol="GOODCO"):
    return db.query(models.TradeDecision).filter_by(symbol=symbol).one()


class TestHelper:
    @pytest.mark.parametrize("atr,expected", [(1.2, "atr"), (0.01, "atr"), (None, "flat"), (0, "flat"), (-1.0, "flat"), ("x", "flat")])
    def test_tag(self, atr, expected):
        assert entry._atr_stop_basis(atr) == expected


class TestWiring:
    def test_no_atr_is_tagged_flat(self, db, monkeypatch):
        make_candidate(db, symbol="GOODCO", conviction=80.0)
        quotes(monkeypatch, {"GOODCO": tick(100.0, atr=None, symbol="GOODCO")})
        approve_all(monkeypatch)
        run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert _dec(db).stop_basis == "flat"

    def test_a_real_atr_is_tagged_atr(self, db, monkeypatch):
        make_candidate(db, symbol="GOODCO", conviction=80.0)
        quotes(monkeypatch, {"GOODCO": tick(100.0, atr=2.0, symbol="GOODCO")})
        approve_all(monkeypatch)
        run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert _dec(db).stop_basis == "atr"

    def test_structural_stop_is_tagged_structure(self, db, monkeypatch):
        monkeypatch.setattr(config, "STRUCTURE_GATE_DEMO_MODE", "enforce")
        monkeypatch.setattr(config, "STRUCTURE_STOP_MODE", "on")
        c = make_candidate(db, symbol="GOODCO", conviction=80.0)
        c.raw_payload = json.dumps({"structure_summary": _sum(ms_stop=96.5)})
        db.commit()
        quotes(monkeypatch, {"GOODCO": tick(100.0, symbol="GOODCO")})
        approve_all(monkeypatch)
        run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert _dec(db).stop_basis == "structure" and _dec(db).proposed_stop == pytest.approx(96.5, abs=0.01)

    def test_an_insane_structural_stop_keeps_the_atr_tag(self, db, monkeypatch):
        monkeypatch.setattr(config, "STRUCTURE_GATE_DEMO_MODE", "enforce")
        monkeypatch.setattr(config, "STRUCTURE_STOP_MODE", "on")
        c = make_candidate(db, symbol="GOODCO", conviction=80.0)
        c.raw_payload = json.dumps({"structure_summary": _sum(ms_stop=80.0, ms_target=140.0)})
        db.commit()
        quotes(monkeypatch, {"GOODCO": tick(100.0, atr=2.0, symbol="GOODCO")})
        approve_all(monkeypatch)
        run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert _dec(db).stop_basis == "atr"


class TestColumn:
    def test_model_has_a_nullable_column(self):
        col = models.TradeDecision.__table__.c.stop_basis
        assert col.nullable and col.type.length == 16

    def test_migration_adds_it_to_a_table_that_predates_it_and_is_idempotent(self):
        eng = create_engine("sqlite:///:memory:")
        with eng.begin() as c:
            c.execute(text("CREATE TABLE trade_positions (id INTEGER PRIMARY KEY)"))
            c.execute(text("CREATE TABLE trade_decisions (id INTEGER PRIMARY KEY, symbol VARCHAR(32))"))
            c.execute(text("CREATE TABLE trade_risk_config (id INTEGER PRIMARY KEY)"))
        dbmod._ensure_cost_model_columns(eng, "postgres")
        assert "stop_basis" in {c["name"] for c in inspect(eng).get_columns("trade_decisions")}
        dbmod._ensure_cost_model_columns(eng, "postgres")
