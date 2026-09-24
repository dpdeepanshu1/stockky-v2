"""
tests/test_entry_evaluate_mode_remaining_coverage_2.py

Second coverage-round batch for entry_engine/entry.py's evaluate_mode() —
closes the remaining scattered gaps left after
test_entry_evaluate_mode_remaining_coverage.py:

  * lines 584-602: Gate 2's "no live tick, but a preview price IS available"
    branch (the sibling of the already-covered "no preview either" branch).
  * lines 681-694: Gate 3 regime-weak WAIT on a candidate that DOES have a
    live tick (as opposed to the pre-loop logging-only path covered in
    round 1, which fires before any candidate is priced).
  * lines 706-710: per_share_risk <= 0 invalid-setup WAIT.
  * line 734: regime-override risk-scale multiplier applied to sizing.
  * lines 739-746: proposed_qty <= 0 (even 1 share exceeds the risk cap) WAIT.
  * lines 755-757: avg_traded_value computation's own except-Exception swallow.
  * lines 817-845: Gate 5.6 cost-model gate, both its sub-reasons (too-small
    trade value, and edge-vs-cost ratio too low).
  * line 886: overnight-priority cycle-ranking bonus.
  * line 895: US-sector-signal cycle-ranking bonus.
  * line 1024: regime-override ENTER reasoning string.
  * line 1129: shared Dhan order-budget exhausted -> RuntimeError -> WAIT.
  * line 1145: cross-service shared-symbol lock held -> RuntimeError -> WAIT.
  * line 1168: Dhan accepts the call but returns no order id -> RuntimeError.
  * lines 1194-1213: invalid-IP Dhan rejection -> auto-disarm REAL + early return.
  * lines 1224-1237: circuit-limit Dhan rejection -> skip-for-today WAIT.

Run from services/real-trade-service:
    python3 -m pytest tests/test_entry_evaluate_mode_remaining_coverage_2.py -q \
        --cov=entry_engine.entry --cov-report=term-missing
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
import cost_model
import models
from entry_engine import entry
from market_feed.feed import Tick
from risk_engine.engine import RiskResult, RiskVerdict


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def pin(monkeypatch):
    for k, v in dict(ENTRY_MIN_REWARD_RISK=2.0, ENTRY_COMPOSITE_WEIGHT_CONVICTION=0.65,
                     ENTRY_COMPOSITE_WEIGHT_RR=0.10, ENTRY_COMPOSITE_WEIGHT_DRIFT=0.25,
                     ENTRY_COMPOSITE_RR_CEILING=4.0, MIN_TRADE_VALUE=3000.0,
                     MIN_EDGE_TO_COST_RATIO=3.0, API_GATEWAY_URL="http://gw",
                     COST_MODEL_ENABLED=False, ENTRY_CYCLE_QUALITY_FILTER_ENABLED=False,
                     ENTRY_MAX_NEW_PER_CYCLE=3, ENTRY_MIN_COMPOSITE_SCORE=50.0,
                     ENTRY_VALIDITY_MINUTES=15, ENTRY_ORDER_TYPE="LIMIT",
                     ENTRY_ZONE_UPPER_PCT=0.1, ENTRY_REGIME_OVERRIDE_TOP_N=0,
                     ENTRY_REGIME_OVERRIDE_RISK_SCALE=0.5,
                     ENTRY_OVERNIGHT_PRIORITY_BONUS=10.0).items():
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
                    signal_price=100.0, mode="DEMO", overnight_priority=False, us_sector_bonus=0.0):
    cand = models.TradeCandidate(
        mode=mode, symbol=symbol, source_tab="hot_picks", decision_label=decision_label,
        conviction_score=conviction, signal_price=signal_price, raw_payload=None,
        consumed=False, overnight_priority=overnight_priority, us_sector_bonus=us_sector_bonus,
    )
    db.add(cand)
    db.commit()
    db.refresh(cand)
    return cand


def tick(price, atr=2.0, symbol="TESTCO", volume=None):
    return Tick(symbol=symbol, price=price, as_of=datetime.now(timezone.utc), atr=atr,
                source="test", volume=volume)


def quotes(monkeypatch, mapping: dict):
    async def _q(symbols):
        return dict(mapping)
    monkeypatch.setattr(entry, "get_quotes", _q)


def preview_quotes(monkeypatch, mapping: dict):
    async def _p(symbols):
        return dict(mapping)
    monkeypatch.setattr(entry, "get_preview_quotes", _p)


def no_preview_quotes(monkeypatch):
    preview_quotes(monkeypatch, {})


def approve_all(monkeypatch, *, approved_qty=None):
    def _fake_risk_evaluate(intent, account_state):
        return RiskResult(verdict=RiskVerdict.APPROVED, check_name="ok", reason="ok",
                           approved_qty=approved_qty or intent.qty)
    monkeypatch.setattr(entry, "risk_evaluate", _fake_risk_evaluate)


async def _async_regime_ok(db):
    return True, 60, 38, "static"


def fake_regime(monkeypatch, *, ok, score=20, threshold=38, src="adaptive"):
    async def _r(db):
        return (ok, score, threshold, src)
    monkeypatch.setattr(entry, "_get_market_regime", _r)


def no_notify(monkeypatch):
    async def _n(*a, **k):
        return None
    monkeypatch.setattr(entry, "notify_async", _n)


def wire_real_dhan_success(monkeypatch, order_id="ORD1"):
    monkeypatch.setattr(entry.dhan_client, "get_security_id", lambda db_, sym: "999")
    monkeypatch.setattr(entry.shared_order_budget, "check_and_reserve", lambda db_: True)
    monkeypatch.setattr(entry.shared_symbol_lock, "try_claim", lambda db_, sym, mode: True)
    monkeypatch.setattr(entry.shared_symbol_lock, "release", lambda db_, sym: None)
    monkeypatch.setattr(entry.dhan_client, "place_order", lambda db_, **kw: {"orderId": order_id})
    no_notify(monkeypatch)


# ── lines 584-602: no live tick, but a preview IS available ─────────────────

class TestGate2PreviewAvailable:
    def test_no_tick_but_preview_available_waits_with_preview_levels(self, db, monkeypatch):
        make_candidate(db, decision_label="BUY NOW")
        quotes(monkeypatch, {})  # no live tick
        preview_quotes(monkeypatch, {"TESTCO": tick(90.0)})  # last-close preview
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["waited"] == 1
        assert "showing preview from last close" in tally["entry_details"][0]["reasoning"]


# ── lines 681-694: regime-weak WAIT with a live tick ─────────────────────────

class TestGate3RegimeWeakWithLiveTick:
    def test_regime_weak_with_live_tick_waits(self, db, monkeypatch):
        fake_regime(monkeypatch, ok=False, score=22, threshold=38)
        make_candidate(db, mode="REAL", decision_label="BUY NOW")
        quotes(monkeypatch, {"TESTCO": tick(100.0)})
        tally = run(entry.evaluate_mode(db, "REAL", gate_armed=True))
        assert tally["waited"] == 1
        assert "Nifty regime is weak" in tally["entry_details"][0]["reasoning"]


# ── lines 706-710: invalid risk setup (stop not below entry) ────────────────

class TestInvalidRiskSetup:
    def test_zero_stop_distance_waits_as_invalid_setup(self, db, monkeypatch):
        fake_regime(monkeypatch, ok=True)
        # Force stop_pct to 0 so stop_price == entry_price -> per_share_risk <= 0.
        monkeypatch.setattr(entry, "_range_adjusted_stop_target", lambda sp, tp, t, ep: (0.0, tp))
        make_candidate(db, decision_label="BUY NOW")
        quotes(monkeypatch, {"TESTCO": tick(100.0)})
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["waited"] == 1
        assert "Computed stop is not below entry" in tally["entry_details"][0]["reasoning"]


# ── line 734 + 1024: regime-override risk-scale sizing + ENTER reasoning ────

class TestRegimeOverride:
    def test_override_candidate_gets_scaled_down_risk_and_enters(self, db, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_REGIME_OVERRIDE_TOP_N", 1)
        fake_regime(monkeypatch, ok=False, score=20, threshold=38)
        make_candidate(db, mode="REAL", decision_label="BUY NOW", conviction=90.0)
        quotes(monkeypatch, {"TESTCO": tick(100.0)})
        approve_all(monkeypatch)
        wire_real_dhan_success(monkeypatch)
        tally = run(entry.evaluate_mode(db, "REAL", gate_armed=True))
        assert tally["entered"] == 1
        decision = db.query(models.TradeDecision).filter_by(symbol="TESTCO").order_by(
            models.TradeDecision.id.desc()).first()
        assert decision is not None
        assert "Regime override" in decision.reasoning


# ── lines 739-746: proposed_qty <= 0 ─────────────────────────────────────────

class TestProposedQtyZero:
    def test_tiny_risk_budget_waits_as_below_one_share(self, db, monkeypatch):
        fake_regime(monkeypatch, ok=True)
        risk = db.query(models.TradeRiskConfig).filter_by(mode="DEMO").first()
        risk.risk_per_trade_pct = 0.00001  # equity*that// per_share_risk == 0
        db.commit()
        make_candidate(db, decision_label="BUY NOW")
        quotes(monkeypatch, {"TESTCO": tick(100.0)})
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["waited"] == 1
        assert "exceeds conviction-adjusted risk cap" in tally["entry_details"][0]["reasoning"]


# ── lines 755-757: avg_traded_value computation exception is swallowed ──────

class TestAvgTradedValueException:
    def test_unusable_volume_field_is_swallowed_and_entry_still_proceeds(self, db, monkeypatch):
        fake_regime(monkeypatch, ok=True)
        make_candidate(db, decision_label="BUY NOW")
        # volume is a non-numeric string -> float(vol) raises inside the try,
        # caught by the function's own `except Exception: pass`.
        quotes(monkeypatch, {"TESTCO": tick(100.0, volume="not-a-number")})
        approve_all(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["entered"] == 1  # must not have crashed


# ── lines 817-845: cost-model gate, both sub-reasons ─────────────────────────

class TestCostModelGate:
    def test_trade_value_too_small_waits_with_min_value_reason(self, db, monkeypatch):
        monkeypatch.setattr(config, "COST_MODEL_ENABLED", True)
        fake_regime(monkeypatch, ok=True)
        make_candidate(db, decision_label="BUY NOW")
        quotes(monkeypatch, {"TESTCO": tick(100.0)})
        approve_all(monkeypatch)
        monkeypatch.setattr(
            entry, "_resolve_cost_gate_knobs", lambda db_, mode: (3000.0, 3.0)
        )
        bad_result = cost_model.EdgeVsCostResult(
            trade_value=500.0, expected_edge=100.0, estimated_cost=20.0, ratio=5.0,
            passes_min_value=False, passes_min_ratio=True,
        )
        monkeypatch.setattr(cost_model, "evaluate_entry_cost_gate", lambda *a, **kw: bad_result)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["waited"] == 1
        assert "too small for fixed" in tally["entry_details"][0]["reasoning"]

    def test_edge_to_cost_ratio_too_low_waits_with_ratio_reason(self, db, monkeypatch):
        monkeypatch.setattr(config, "COST_MODEL_ENABLED", True)
        fake_regime(monkeypatch, ok=True)
        make_candidate(db, decision_label="BUY NOW")
        quotes(monkeypatch, {"TESTCO": tick(100.0)})
        approve_all(monkeypatch)
        monkeypatch.setattr(
            entry, "_resolve_cost_gate_knobs", lambda db_, mode: (3000.0, 3.0)
        )
        bad_result = cost_model.EdgeVsCostResult(
            trade_value=5000.0, expected_edge=30.0, estimated_cost=25.0, ratio=1.2,
            passes_min_value=True, passes_min_ratio=False,
        )
        monkeypatch.setattr(cost_model, "evaluate_entry_cost_gate", lambda *a, **kw: bad_result)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["waited"] == 1
        assert "Not enough real edge left" in tally["entry_details"][0]["reasoning"]


# ── line 886 / 895: overnight-priority + US-sector ranking bonuses ──────────

class TestRankingBonuses:
    def test_overnight_priority_candidate_still_enters(self, db, monkeypatch):
        fake_regime(monkeypatch, ok=True)
        cand = make_candidate(db, decision_label="BUY NOW")
        cand.overnight_priority = True
        db.commit()
        quotes(monkeypatch, {"TESTCO": tick(100.0)})
        approve_all(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["entered"] == 1

    def test_us_sector_bonus_candidate_still_enters(self, db, monkeypatch):
        fake_regime(monkeypatch, ok=True)
        cand = make_candidate(db, decision_label="BUY NOW")
        cand.us_sector_bonus = 3.5
        db.commit()
        quotes(monkeypatch, {"TESTCO": tick(100.0)})
        approve_all(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["entered"] == 1


# ── line 1129: shared Dhan order-budget exhausted ────────────────────────────

class TestSharedOrderBudgetExhausted:
    def test_budget_exhausted_waits_instead_of_entering(self, db, monkeypatch):
        make_candidate(db, symbol="REALCO", mode="REAL")
        quotes(monkeypatch, {"REALCO": tick(100.0, symbol="REALCO")})
        approve_all(monkeypatch)
        monkeypatch.setattr(entry, "_get_market_regime", _async_regime_ok)
        monkeypatch.setattr(entry.dhan_client, "get_security_id", lambda db_, sym: "999")
        monkeypatch.setattr(entry.shared_order_budget, "check_and_reserve", lambda db_: False)
        monkeypatch.setattr(entry.shared_symbol_lock, "release", lambda db_, sym: None)
        no_notify(monkeypatch)
        tally = run(entry.evaluate_mode(db, "REAL", gate_armed=True))
        assert tally["entered"] == 0
        assert tally["waited"] == 1
        order = db.query(models.TradeOrder).filter_by(symbol="REALCO").one()
        assert order.status == "REJECTED"
        events = db.query(models.TradeOrderEvent).filter_by(order_id=order.id).all()
        assert any("budget exhausted" in (e.detail or "") for e in events)


# ── line 1145: cross-service shared-symbol lock already held ────────────────

class TestSharedSymbolLockHeld:
    def test_symbol_already_held_by_other_service_waits(self, db, monkeypatch):
        make_candidate(db, symbol="REALCO", mode="REAL")
        quotes(monkeypatch, {"REALCO": tick(100.0, symbol="REALCO")})
        approve_all(monkeypatch)
        monkeypatch.setattr(entry, "_get_market_regime", _async_regime_ok)
        monkeypatch.setattr(entry.dhan_client, "get_security_id", lambda db_, sym: "999")
        monkeypatch.setattr(entry.shared_order_budget, "check_and_reserve", lambda db_: True)
        monkeypatch.setattr(entry.shared_symbol_lock, "try_claim", lambda db_, sym, mode: False)
        monkeypatch.setattr(entry.shared_symbol_lock, "release", lambda db_, sym: None)
        no_notify(monkeypatch)
        tally = run(entry.evaluate_mode(db, "REAL", gate_armed=True))
        assert tally["entered"] == 0
        assert tally["waited"] == 1
        order = db.query(models.TradeOrder).filter_by(symbol="REALCO").one()
        events = db.query(models.TradeOrderEvent).filter_by(order_id=order.id).all()
        assert any("already held by position-stocks-service" in (e.detail or "") for e in events)


# ── line 1168: Dhan accepts but returns no order id ──────────────────────────

class TestDhanNoOrderId:
    def test_missing_order_id_waits(self, db, monkeypatch):
        make_candidate(db, symbol="REALCO", mode="REAL")
        quotes(monkeypatch, {"REALCO": tick(100.0, symbol="REALCO")})
        approve_all(monkeypatch)
        monkeypatch.setattr(entry, "_get_market_regime", _async_regime_ok)
        monkeypatch.setattr(entry.dhan_client, "get_security_id", lambda db_, sym: "999")
        monkeypatch.setattr(entry.shared_order_budget, "check_and_reserve", lambda db_: True)
        monkeypatch.setattr(entry.shared_symbol_lock, "try_claim", lambda db_, sym, mode: True)
        monkeypatch.setattr(entry.shared_symbol_lock, "release", lambda db_, sym: None)
        monkeypatch.setattr(entry.dhan_client, "place_order", lambda db_, **kw: {})  # no orderId
        no_notify(monkeypatch)
        tally = run(entry.evaluate_mode(db, "REAL", gate_armed=True))
        assert tally["entered"] == 0
        assert tally["waited"] == 1
        order = db.query(models.TradeOrder).filter_by(symbol="REALCO").one()
        events = db.query(models.TradeOrderEvent).filter_by(order_id=order.id).all()
        assert any("no order id" in (e.detail or "") for e in events)


# ── lines 1194-1213: invalid-IP rejection auto-disarms REAL ─────────────────

class TestInvalidIpAutoDisarm:
    def test_invalid_ip_error_auto_disarms_and_returns_early(self, db, monkeypatch):
        make_candidate(db, symbol="REALCO", mode="REAL")
        quotes(monkeypatch, {"REALCO": tick(100.0, symbol="REALCO")})
        approve_all(monkeypatch)
        monkeypatch.setattr(entry, "_get_market_regime", _async_regime_ok)
        monkeypatch.setattr(entry.dhan_client, "get_security_id", lambda db_, sym: "999")
        monkeypatch.setattr(entry.shared_order_budget, "check_and_reserve", lambda db_: True)
        monkeypatch.setattr(entry.shared_symbol_lock, "try_claim", lambda db_, sym, mode: True)
        monkeypatch.setattr(entry.shared_symbol_lock, "release", lambda db_, sym: None)

        def _boom(db_, **kw):
            raise RuntimeError("DH-907: invalid IP address")
        monkeypatch.setattr(entry.dhan_client, "place_order", _boom)

        disarm_calls = []
        monkeypatch.setattr(
            "auth.dhan_credentials.disarm_on_invalid_ip",
            lambda db_, mode, err: disarm_calls.append((mode, err)) or True,
        )
        no_notify(monkeypatch)
        tally = run(entry.evaluate_mode(db, "REAL", gate_armed=True))
        assert tally.get("auto_disarmed") == "invalid_ip"
        assert tally["entered"] == 0
        assert tally["waited"] == 1
        assert len(disarm_calls) == 1
        order = db.query(models.TradeOrder).filter_by(symbol="REALCO").one()
        assert order.status == "REJECTED"


# ── lines 1224-1237: circuit-limit rejection skips for today ────────────────

class TestCircuitLimitSkip:
    def test_circuit_limit_error_waits_and_skips_for_today(self, db, monkeypatch):
        make_candidate(db, symbol="REALCO", mode="REAL")
        quotes(monkeypatch, {"REALCO": tick(100.0, symbol="REALCO")})
        approve_all(monkeypatch)
        monkeypatch.setattr(entry, "_get_market_regime", _async_regime_ok)
        monkeypatch.setattr(entry.dhan_client, "get_security_id", lambda db_, sym: "999")
        monkeypatch.setattr(entry.shared_order_budget, "check_and_reserve", lambda db_: True)
        monkeypatch.setattr(entry.shared_symbol_lock, "try_claim", lambda db_, sym, mode: True)
        monkeypatch.setattr(entry.shared_symbol_lock, "release", lambda db_, sym: None)

        def _boom(db_, **kw):
            raise RuntimeError("RMS:...:Rate Not Within Ckt Limit 395.25 To 592.85")
        monkeypatch.setattr(entry.dhan_client, "place_order", _boom)
        no_notify(monkeypatch)
        tally = run(entry.evaluate_mode(db, "REAL", gate_armed=True))
        assert tally["entered"] == 0
        assert tally["waited"] == 1
        order = db.query(models.TradeOrder).filter_by(symbol="REALCO").one()
        assert order.status == "REJECTED"
