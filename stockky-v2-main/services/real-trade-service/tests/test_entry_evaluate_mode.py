"""
tests/test_entry_evaluate_mode.py — offline tests for entry_engine.entry.evaluate_mode(),
the ~830-line candidate-to-order pipeline that decides every real BUY.

Previously untested (0% direct coverage of evaluate_mode's body — see
test_rt_entry_helpers.py's own docstring, which explicitly scopes it out, and
AUDIT_REPORT.md's "still untested" table). Focuses on DEMO mode, which exercises the
same gate/ranking/staging logic as REAL without needing Dhan/broker mocking (REAL's
extra Dhan-placement branch already has some coverage via the entry-order-lifecycle
tests in test_rt_entry_helpers.py; this file adds one focused REAL routing check on
top of the DEMO-mode gate coverage). risk_engine.evaluate is mocked at the boundary
(same pattern as exit.py's _send_real_sell mocking in test_exit_evaluate_mode.py) —
risk_engine has its own dedicated test suite and its own live-market-hours
dependency (AccountState.market_is_open), which would make it a flaky, wrong thing
to re-exercise indirectly from here.

Run from services/real-trade-service:
    python3 -m pytest tests/test_entry_evaluate_mode.py -q --cov=entry_engine.entry --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    # Same pinning as test_rt_entry_helpers.py's `pin` fixture, so the two
    # files' expectations about gate thresholds never drift apart.
    for k, v in dict(ENTRY_MIN_REWARD_RISK=2.0, ENTRY_COMPOSITE_WEIGHT_CONVICTION=0.65,
                     ENTRY_COMPOSITE_WEIGHT_RR=0.10, ENTRY_COMPOSITE_WEIGHT_DRIFT=0.25,
                     ENTRY_COMPOSITE_RR_CEILING=4.0, MIN_TRADE_VALUE=3000.0,
                     MIN_EDGE_TO_COST_RATIO=3.0, API_GATEWAY_URL="http://gw",
                     COST_MODEL_ENABLED=False, ENTRY_CYCLE_QUALITY_FILTER_ENABLED=False,
                     ENTRY_MAX_NEW_PER_CYCLE=3, ENTRY_MIN_COMPOSITE_SCORE=50.0,
                     ENTRY_VALIDITY_MINUTES=15, ENTRY_ORDER_TYPE="LIMIT",
                     ENTRY_ZONE_UPPER_PCT=0.1, ENTRY_REGIME_OVERRIDE_TOP_N=0).items():
        monkeypatch.setattr(config, k, v)
    for k, v in dict(MIN_REWARD_RISK_RATIO=2.0, MAX_ENTRY_DRIFT_ATR=0.75, CONVICTION_MIDPOINT=65.0,
                     CONVICTION_MAX_SCALE=0.25, REGIME_MIN_SCORE_STATIC=38).items():
        monkeypatch.setattr(entry, k, v)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    s.add(models.TradeAccount(mode="DEMO", starting_capital=100000.0,
                               current_equity=100000.0, cash_available=100000.0))
    s.add(models.TradeAccount(mode="REAL", starting_capital=100000.0,
                               current_equity=100000.0, cash_available=100000.0))
    s.add(models.TradeRiskConfig(mode="DEMO"))
    s.add(models.TradeRiskConfig(mode="REAL"))
    s.commit()
    yield s
    s.close()


def make_candidate(db, *, symbol="TESTCO", decision_label="BUY NOW", conviction=70.0,
                    signal_price=100.0, mode="DEMO"):
    cand = models.TradeCandidate(
        mode=mode, symbol=symbol, source_tab="hot_picks", decision_label=decision_label,
        conviction_score=conviction, signal_price=signal_price, raw_payload=None,
        consumed=False,
    )
    db.add(cand)
    db.commit()
    db.refresh(cand)
    return cand


def tick(price, atr=2.0, symbol="TESTCO"):
    return Tick(symbol=symbol, price=price, as_of=datetime.now(timezone.utc), atr=atr, source="test")


def quotes(monkeypatch, mapping: dict):
    async def _q(symbols):
        return dict(mapping)
    monkeypatch.setattr(entry, "get_quotes", _q)


def approve_all(monkeypatch, *, approved_qty=None):
    def _fake_risk_evaluate(intent, account_state):
        return RiskResult(verdict=RiskVerdict.APPROVED, check_name="ok", reason="ok",
                           approved_qty=approved_qty or intent.qty)
    monkeypatch.setattr(entry, "risk_evaluate", _fake_risk_evaluate)


def reject_all(monkeypatch, reason="capped"):
    def _fake_risk_evaluate(intent, account_state):
        return RiskResult(verdict=RiskVerdict.REJECTED, check_name="cap", reason=reason)
    monkeypatch.setattr(entry, "risk_evaluate", _fake_risk_evaluate)


class TestEmptyAndNoTick:
    def test_no_unconsumed_candidates_returns_zeroed_tally(self, db):
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally == {"evaluated": 0, "entered": 0, "waited": 0, "rejected": 0, "entry_details": []}

    def test_candidate_marked_consumed_even_on_wait(self, db, monkeypatch):
        cand = make_candidate(db, decision_label="HOLD")  # not actionable
        run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        db.refresh(cand)
        assert cand.consumed is True


class TestGate1ActionableLabel:
    def test_non_actionable_label_waits(self, db, monkeypatch):
        make_candidate(db, decision_label="HOLD")
        quotes(monkeypatch, {})
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["waited"] == 1
        assert tally["entered"] == 0
        assert "not actionable" in tally["entry_details"][0]["reasoning"]

    @pytest.mark.parametrize("label", ["BUY NOW", "PREPARE TO BUY", "VOLUME_SHOCK_HIGH_CONVICTION"])
    def test_actionable_labels_pass_gate1(self, db, monkeypatch, label):
        make_candidate(db, decision_label=label)
        quotes(monkeypatch, {"TESTCO": tick(100.0)})
        approve_all(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        # Gate 1 passed means it did NOT wait with the gate-1 message (an
        # ENTER decision's reasoning is None unless it's a regime override).
        reasoning = tally["entry_details"][0]["reasoning"] or ""
        assert "not actionable" not in reasoning


class TestGate1bVolumeShockBaseTier:
    def test_base_volume_shock_waits_when_disabled(self, db, monkeypatch):
        monkeypatch.setattr(entry, "VOLUME_SHOCK_BASE_TIER_AUTO_ENTRY_ENABLED", False)
        make_candidate(db, decision_label="VOLUME_SHOCK")
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["waited"] == 1
        assert "Base VOLUME_SHOCK tier auto-entry is off" in tally["entry_details"][0]["reasoning"]

    def test_base_volume_shock_proceeds_when_enabled(self, db, monkeypatch):
        monkeypatch.setattr(entry, "VOLUME_SHOCK_BASE_TIER_AUTO_ENTRY_ENABLED", True)
        make_candidate(db, decision_label="VOLUME_SHOCK")
        quotes(monkeypatch, {"TESTCO": tick(100.0)})
        approve_all(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        reasoning = tally["entry_details"][0]["reasoning"] or ""
        assert "Base VOLUME_SHOCK" not in reasoning


class TestGate2NoTick:
    def test_no_live_tick_waits_and_never_enters(self, db, monkeypatch):
        make_candidate(db)
        quotes(monkeypatch, {})

        async def _no_preview(symbols):
            return {}
        monkeypatch.setattr(entry, "get_preview_quotes", _no_preview)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["waited"] == 1
        assert tally["entered"] == 0


class TestGate4Drift:
    def test_price_run_far_above_signal_waits_as_chasing(self, db, monkeypatch):
        make_candidate(db, signal_price=100.0)
        # ATR 2.0 -> max_drift = 2.0*0.75=1.5%; price +10% is way beyond that
        quotes(monkeypatch, {"TESTCO": tick(110.0, atr=2.0)})
        approve_all(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["entered"] == 0
        assert tally["waited"] == 1
        assert "Chasing" in tally["entry_details"][0]["reasoning"] or "ran +" in tally["entry_details"][0]["reasoning"]


class TestGate5RewardRiskFloor:
    def test_rr_below_floor_waits(self, db, monkeypatch):
        # Flat-fallback stop/target (no ATR) gives ~2.03:1 by design, which
        # clears a 2.0 floor -- so force ATR small/negative to break R:R via
        # a very tight target instead: use a regime where MIN_REWARD_RISK_RATIO
        # is raised above what the flat fallback can deliver.
        monkeypatch.setattr(entry, "MIN_REWARD_RISK_RATIO", 100.0)
        make_candidate(db)
        quotes(monkeypatch, {"TESTCO": tick(100.0, atr=None)})
        approve_all(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["entered"] == 0
        assert "R:R" in tally["entry_details"][0]["reasoning"]


class TestRiskEngineGate:
    def test_risk_rejection_waits_with_risk_engines_own_reason(self, db, monkeypatch):
        make_candidate(db)
        quotes(monkeypatch, {"TESTCO": tick(100.0)})
        reject_all(monkeypatch, reason="Daily loss cap hit")
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["entered"] == 0
        assert tally["waited"] == 1
        assert tally["entry_details"][0]["reasoning"] == "Daily loss cap hit"


class TestDuplicateSymbolGuard:
    def test_two_candidates_same_symbol_only_one_staged(self, db, monkeypatch):
        make_candidate(db, symbol="DUPCO")
        c2 = models.TradeCandidate(
            mode="DEMO", symbol="DUPCO", source_tab="hot_picks", decision_label="BUY NOW",
            conviction_score=70.0, signal_price=100.0, raw_payload=None, consumed=False,
        )
        db.add(c2)
        db.commit()
        quotes(monkeypatch, {"DUPCO": tick(100.0, symbol="DUPCO")})
        approve_all(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["entered"] == 1
        assert tally["waited"] == 1
        reasons = [d["reasoning"] for d in tally["entry_details"]]
        assert any("already approved and staged" in r for r in reasons)


class TestDemoEntersAndPlacesOrder:
    def test_approved_candidate_enters_and_creates_a_placed_order(self, db, monkeypatch):
        make_candidate(db, symbol="GOODCO", conviction=80.0)
        quotes(monkeypatch, {"GOODCO": tick(100.0, symbol="GOODCO")})
        approve_all(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["entered"] == 1
        assert tally["waited"] == 0
        order = db.query(models.TradeOrder).filter_by(symbol="GOODCO").one()
        assert order.side == "BUY"
        assert order.status == "PLACED"
        assert order.mode == "DEMO"
        assert order.qty > 0
        decision = db.query(models.TradeDecision).filter_by(symbol="GOODCO").one()
        assert decision.action == "ENTER"

    def test_demo_never_calls_dhan(self, db, monkeypatch):
        make_candidate(db, symbol="GOODCO")
        quotes(monkeypatch, {"GOODCO": tick(100.0, symbol="GOODCO")})
        approve_all(monkeypatch)

        called = {"n": 0}
        def _boom(*a, **k):
            called["n"] += 1
            raise AssertionError("DEMO mode must never call the broker")
        monkeypatch.setattr(entry.dhan_client, "place_order", _boom)

        run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert called["n"] == 0


class TestGate6QualityRanking:
    def test_below_composite_floor_is_dropped_when_filter_enabled(self, db, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_CYCLE_QUALITY_FILTER_ENABLED", True)
        monkeypatch.setattr(config, "ENTRY_MIN_COMPOSITE_SCORE", 999.0)  # impossible to clear
        make_candidate(db, symbol="WEAKCO", conviction=10.0)
        quotes(monkeypatch, {"WEAKCO": tick(100.0, symbol="WEAKCO")})
        approve_all(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["entered"] == 0
        assert tally["waited"] == 1
        assert "composite" in tally["entry_details"][0]["reasoning"]

    def test_upper_circuit_bypasses_composite_floor(self, db, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_CYCLE_QUALITY_FILTER_ENABLED", True)
        monkeypatch.setattr(config, "ENTRY_MIN_COMPOSITE_SCORE", 999.0)
        make_candidate(db, symbol="UCCO", decision_label="VOLUME_SHOCK_UPPER_CIRCUIT", conviction=10.0)
        quotes(monkeypatch, {"UCCO": tick(100.0, symbol="UCCO")})
        approve_all(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["entered"] == 1  # UC bypasses the floor entirely

    def test_max_new_per_cycle_caps_entries_ranking_by_composite(self, db, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_CYCLE_QUALITY_FILTER_ENABLED", True)
        monkeypatch.setattr(config, "ENTRY_MIN_COMPOSITE_SCORE", 0.0)
        monkeypatch.setattr(config, "ENTRY_MAX_NEW_PER_CYCLE", 1)
        make_candidate(db, symbol="AAA", conviction=90.0)
        make_candidate(db, symbol="BBB", conviction=20.0)
        quotes(monkeypatch, {
            "AAA": tick(100.0, symbol="AAA"), "BBB": tick(100.0, symbol="BBB"),
        })
        approve_all(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["entered"] == 1
        entered_symbols = {
            o.symbol for o in db.query(models.TradeOrder).filter_by(status="PLACED").all()
        }
        assert entered_symbols == {"AAA"}  # higher conviction -> higher composite -> wins the slot


class TestRealModeRoutesToDhan:
    def test_real_approved_entry_places_order_via_dhan_client(self, db, monkeypatch):
        make_candidate(db, symbol="REALCO", mode="REAL")
        quotes(monkeypatch, {"REALCO": tick(100.0, symbol="REALCO")})
        approve_all(monkeypatch)
        monkeypatch.setattr(entry, "_get_market_regime", _async_regime_ok)
        monkeypatch.setattr(entry.dhan_client, "get_security_id", lambda db_, sym: "999")
        monkeypatch.setattr(entry.shared_order_budget, "check_and_reserve", lambda db_: True)
        monkeypatch.setattr(entry.shared_symbol_lock, "try_claim", lambda db_, sym, mode: True)
        monkeypatch.setattr(entry.dhan_client, "place_order", lambda db_, **kw: {"orderId": "ORD1"})

        async def _no_notify(*a, **k):
            return None
        monkeypatch.setattr(entry, "notify_async", _no_notify)

        tally = run(entry.evaluate_mode(db, "REAL", gate_armed=True))
        assert tally["entered"] == 1
        order = db.query(models.TradeOrder).filter_by(symbol="REALCO").one()
        assert order.dhan_order_id == "ORD1"

    def test_real_dhan_placement_failure_waits_instead_of_entering(self, db, monkeypatch):
        make_candidate(db, symbol="REALCO", mode="REAL")
        quotes(monkeypatch, {"REALCO": tick(100.0, symbol="REALCO")})
        approve_all(monkeypatch)
        monkeypatch.setattr(entry, "_get_market_regime", _async_regime_ok)
        monkeypatch.setattr(entry.dhan_client, "get_security_id", lambda db_, sym: "999")
        monkeypatch.setattr(entry.shared_order_budget, "check_and_reserve", lambda db_: True)
        monkeypatch.setattr(entry.shared_symbol_lock, "try_claim", lambda db_, sym, mode: True)
        monkeypatch.setattr(entry.shared_symbol_lock, "release", lambda db_, sym: None)

        def _boom(db_, **kw):
            raise RuntimeError("Dhan rejected: some reason")
        monkeypatch.setattr(entry.dhan_client, "place_order", _boom)

        async def _no_notify(*a, **k):
            return None
        monkeypatch.setattr(entry, "notify_async", _no_notify)

        tally = run(entry.evaluate_mode(db, "REAL", gate_armed=True))
        assert tally["entered"] == 0
        assert tally["waited"] == 1
        order = db.query(models.TradeOrder).filter_by(symbol="REALCO").one()
        assert order.status == "REJECTED"


async def _async_regime_ok(db):
    return True, 60, 38, "static"
