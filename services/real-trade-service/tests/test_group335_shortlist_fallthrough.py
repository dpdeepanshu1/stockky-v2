"""group335 - real-trade Gate 6: when a selected pick's broker placement fails for a reason about that symbol, the next shortlisted pick
is placed in its slot in the same cycle. Account-wide failures (shared order budget) and ENTRY_SHORTLIST_FALLTHROUGH=false do not promote.
Run from services/real-trade-service:  python3 -m pytest tests/test_group335_shortlist_fallthrough.py -q"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from entry_engine import entry
from market_feed.feed import Tick
from risk_engine.engine import RiskResult, RiskVerdict


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def pin(monkeypatch):
    for k, v in dict(ENTRY_MIN_REWARD_RISK=2.0, ENTRY_COMPOSITE_WEIGHT_CONVICTION=0.65, ENTRY_COMPOSITE_WEIGHT_RR=0.10,
                     ENTRY_COMPOSITE_WEIGHT_DRIFT=0.25, ENTRY_COMPOSITE_RR_CEILING=4.0, MIN_TRADE_VALUE=3000.0,
                     MIN_EDGE_TO_COST_RATIO=3.0, API_GATEWAY_URL="http://gw", COST_MODEL_ENABLED=False,
                     ENTRY_CYCLE_QUALITY_FILTER_ENABLED=True, ENTRY_SHORTLIST_ENABLED=True, ENTRY_SHORTLIST_MAX=4,
                     ENTRY_SHORTLIST_FALLTHROUGH=True, ENTRY_MAX_NEW_PER_CYCLE=1, ENTRY_MIN_COMPOSITE_SCORE=0.0,
                     ENTRY_VALIDITY_MINUTES=15, ENTRY_ORDER_TYPE="LIMIT", ENTRY_ZONE_UPPER_PCT=0.1,
                     ENTRY_REGIME_OVERRIDE_TOP_N=0).items():
        monkeypatch.setattr(config, k, v)
    for k, v in dict(MIN_REWARD_RISK_RATIO=2.0, MAX_ENTRY_DRIFT_ATR=0.75, CONVICTION_MIDPOINT=65.0,
                     CONVICTION_MAX_SCALE=0.25, REGIME_MIN_SCORE_STATIC=38).items():
        monkeypatch.setattr(entry, k, v)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    for m in ("DEMO", "REAL"):
        s.add(models.TradeAccount(mode=m, starting_capital=1_000_000.0, current_equity=1_000_000.0, cash_available=1_000_000.0))
        s.add(models.TradeRiskConfig(mode=m))
    s.commit()
    yield s
    s.close()


def _cand(db, symbol, conviction):
    c = models.TradeCandidate(mode="REAL", symbol=symbol, source_tab="hot_picks", decision_label="BUY NOW",
                              conviction_score=conviction, signal_price=100.0, raw_payload=None, consumed=False)
    db.add(c)
    db.commit()


def _wire(monkeypatch, place_order):
    ticks = {s: Tick(symbol=s, price=100.0, as_of=datetime.now(timezone.utc), atr=2.0, source="test") for s in ("AAA", "BBB", "CCC", "DDD")}

    async def _q(symbols):
        return dict(ticks)

    async def _regime(db_):
        return True, 60, 38, "static"

    async def _rows():
        return {}

    async def _n(*a, **k):
        return None

    monkeypatch.setattr(entry, "get_quotes", _q)
    monkeypatch.setattr(entry, "_get_market_regime", _regime)
    monkeypatch.setattr(entry._board_client, "get_rows", _rows)
    monkeypatch.setattr(entry, "notify_async", _n)
    monkeypatch.setattr(entry, "risk_evaluate", lambda intent, acct: RiskResult(
        verdict=RiskVerdict.APPROVED, check_name="ok", reason="ok", approved_qty=intent.qty))
    monkeypatch.setattr(entry.dhan_client, "get_security_id", lambda db_, sym: "ID_" + sym)
    monkeypatch.setattr(entry.shared_order_budget, "check_and_reserve", lambda db_: True)
    monkeypatch.setattr(entry.shared_symbol_lock, "try_claim", lambda db_, sym, mode: True)
    monkeypatch.setattr(entry.shared_symbol_lock, "release", lambda db_, sym: None)
    monkeypatch.setattr(entry.dhan_client, "place_order", place_order)


def _four(db):
    _cand(db, "AAA", 90.0)
    _cand(db, "BBB", 80.0)
    _cand(db, "CCC", 70.0)
    _cand(db, "DDD", 66.0)


def _actions(tally):
    return {d["symbol"]: d["action"] for d in tally["entry_details"]}


def test_failed_best_pick_hands_its_slot_to_the_next_pick(db, monkeypatch):
    placed = []

    def place(db_, **kw):
        if kw["security_id"] == "ID_AAA":
            raise RuntimeError("DH-906: order rejected for this security")
        placed.append(kw["security_id"])
        return {"orderId": "ORD-" + kw["security_id"]}

    _wire(monkeypatch, place)
    _four(db)
    tally = run(entry.evaluate_mode(db, "REAL", gate_armed=True))
    assert tally["entered"] == 1
    assert placed == ["ID_BBB"]                       # the second-best took the slot; CCC / DDD were not needed
    assert _actions(tally) == {"AAA": "WAIT", "BBB": "ENTER", "CCC": "WAIT", "DDD": "WAIT"}
    assert [d["symbol"] for d in tally["entry_details"]].count("BBB") == 1


def test_shared_order_budget_exhaustion_does_not_promote(db, monkeypatch):
    placed = []
    _wire(monkeypatch, lambda db_, **kw: placed.append(kw) or {"orderId": "X"})
    monkeypatch.setattr(entry.shared_order_budget, "check_and_reserve", lambda db_: False)
    _four(db)
    tally = run(entry.evaluate_mode(db, "REAL", gate_armed=True))
    assert tally["entered"] == 0 and placed == []
    assert _actions(tally)["BBB"] == "WAIT"


def test_flag_off_keeps_old_behaviour(db, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_SHORTLIST_FALLTHROUGH", False)
    placed = []

    def place(db_, **kw):
        if kw["security_id"] == "ID_AAA":
            raise RuntimeError("DH-906: order rejected for this security")
        placed.append(kw["security_id"])
        return {"orderId": "X"}

    _wire(monkeypatch, place)
    _four(db)
    tally = run(entry.evaluate_mode(db, "REAL", gate_armed=True))
    assert tally["entered"] == 0 and placed == []
    assert _actions(tally) == {"AAA": "WAIT", "BBB": "WAIT", "CCC": "WAIT", "DDD": "WAIT"}


def test_slots_are_never_exceeded(db, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_MAX_NEW_PER_CYCLE", 2)
    placed = []

    def place(db_, **kw):
        placed.append(kw["security_id"])
        return {"orderId": "X" + kw["security_id"]}

    _wire(monkeypatch, place)
    _four(db)
    tally = run(entry.evaluate_mode(db, "REAL", gate_armed=True))
    assert tally["entered"] == 2 and placed == ["ID_AAA", "ID_BBB"]
    assert _actions(tally)["CCC"] == "WAIT" and _actions(tally)["DDD"] == "WAIT"
