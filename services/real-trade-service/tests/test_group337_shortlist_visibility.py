"""group337 - real-trade remembers each cycle's shortlist and what became of every pick and serves it (entry_engine.shortlist.snapshot, GET /shortlist/{mode});
DEMO ranks with the same Market Board nudge as REAL.
Run from services/real-trade-service:  python3 -m pytest tests/test_group337_shortlist_visibility.py -q"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import config
import models
from entry_engine import entry, shortlist
from tests.test_group335_shortlist_fallthrough import (  # noqa: F401  (fixtures + helpers of the neighbouring test module)
    _cand, _four, _wire, _actions, db, pin, run,
)


@pytest.fixture(autouse=True)
def _clean():
    shortlist.reset()
    yield
    shortlist.reset()


def _e(sym, score, **kw):
    return {"cand": SimpleNamespace(symbol=sym, conviction_score=80, decision_label="BUY NOW"), "composite_score": score,
            "raw_composite_score": score - kw.get("board_bonus", 0.0), "is_upper_circuit": False, "rr": 2.5,
            "entry_price": 100.0, "stop_price": 98.0, "target_price": 105.0, **kw}


class TestState:
    def test_record_marks_selected_and_reserve(self, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_MIN_COMPOSITE_SCORE", 0.0)
        monkeypatch.setattr(config, "ENTRY_MAX_NEW_PER_CYCLE", 1)
        monkeypatch.setattr(config, "ENTRY_SHORTLIST_MAX", 3)
        approved = [_e("A", 90, board_bonus=4.0), _e("B", 80), _e("C", 70), _e("D", 60)]
        built = shortlist.build(approved)
        selected = built["selected"]
        reserve = [x for x in built["shortlist"] if x not in selected]
        shortlist.record("REAL", built, len(approved), selected, reserve)
        snap = shortlist.snapshot("REAL")
        assert [(p["symbol"], p["status"]) for p in snap["picks"]] == [("A", "SELECTED"), ("B", "RESERVE"), ("C", "RESERVE")]
        assert snap["approved_count"] == 4 and snap["picks"][0]["board_bonus"] == 4.0 and snap["mode"] == "REAL"
        assert shortlist.snapshot("DEMO")["picks"] == []                   # modes are separate

    def test_mark_and_finalize(self, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_MIN_COMPOSITE_SCORE", 0.0)
        monkeypatch.setattr(config, "ENTRY_MAX_NEW_PER_CYCLE", 1)
        approved = [_e("A", 90), _e("B", 80)]
        built = shortlist.build(approved)
        shortlist.record("REAL", built, 2, built["selected"], [x for x in built["shortlist"] if x not in built["selected"]])
        shortlist.mark("REAL", "A", "ENTERED")
        shortlist.finalize("REAL")
        assert {p["symbol"]: p["outcome"] for p in shortlist.snapshot("REAL")["picks"]} == {"A": "ENTERED", "B": "NOT_NEEDED"}

    def test_never_raises_on_garbage(self):
        shortlist.record("REAL", None, 1, [], [])           # type: ignore[arg-type]
        shortlist.mark("REAL", None, "x")
        shortlist.finalize("NOPE")
        assert shortlist.snapshot("NOPE")["picks"] == []


class TestInACycle:
    def test_failed_best_pick_is_recorded_and_next_pick_enters(self, db, monkeypatch):
        def place(db_, **kw):
            if kw["security_id"] == "ID_AAA":
                raise RuntimeError("DH-906: order rejected for this security")
            return {"orderId": "ORD-" + kw["security_id"]}

        _wire(monkeypatch, place)
        _four(db)
        run(entry.evaluate_mode(db, "REAL", gate_armed=True))
        snap = shortlist.snapshot("REAL")
        out = {p["symbol"]: p["outcome"] for p in snap["picks"]}
        assert out["AAA"].startswith("PLACEMENT_FAILED") and out["BBB"] == "ENTERED"
        assert out["CCC"] == "NOT_NEEDED" and out["DDD"] == "NOT_NEEDED"
        assert [p["status"] for p in snap["picks"]][:2] == ["SELECTED", "RESERVE"]
        assert snap["approved_count"] == 4 and snap["max"] == 4

    def test_demo_mode_asks_the_board_too(self, db, monkeypatch):
        asked = []

        async def _rows():
            asked.append(1)
            return {}

        _wire(monkeypatch, lambda db_, **kw: {"orderId": "X"})
        monkeypatch.setattr(entry._board_client, "get_rows", _rows)
        db.add(models.TradeCandidate(mode="DEMO", symbol="AAA", source_tab="hot_picks", decision_label="BUY NOW", conviction_score=90.0,
                                     signal_price=100.0, raw_payload=None, consumed=False))
        db.commit()
        run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert asked, "DEMO ranked without reading the board nudge"
