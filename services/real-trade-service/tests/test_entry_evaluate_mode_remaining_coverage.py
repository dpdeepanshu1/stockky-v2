"""
tests/test_entry_evaluate_mode_remaining_coverage.py

Coverage-round tests for entry_engine/entry.py's evaluate_mode() — closes
three small gaps left after test_entry_evaluate_mode.py and
test_rt_entry_helpers.py (which both focus on the gate/ranking pipeline
itself, not these three defensive/logging branches):

  * lines 458-463: the REAL-mode regime-WEAK logging path, including both
    the `try: from adaptive_thresholds import threshold_age_note` success
    case and its `except Exception: age_note = ""` fallback — mirrors the
    existing test_exit_clamp_for_atr_importerror_fallback.py pattern, but
    here the try/except is call-time (not import-time), so a plain
    monkeypatch on the target function is enough; no module reload needed.
  * lines 480-482: get_preview_quotes() raising for symbols with no live
    tick — must be swallowed (non-fatal) rather than aborting the cycle.
  * lines 536-537: pstat.set_symbol_progress() raising — must be swallowed
    (it's a best-effort progress-bar update, never allowed to break entry
    evaluation).

Run from services/real-trade-service:
    python3 -m pytest tests/test_entry_evaluate_mode_remaining_coverage.py -q \
        --cov=entry_engine.entry --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import logging
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
    # Same pinning as test_entry_evaluate_mode.py's `pin` fixture.
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


def fake_regime(monkeypatch, *, ok, score=20, threshold=38, src="adaptive"):
    async def _r(db):
        return (ok, score, threshold, src)
    monkeypatch.setattr(entry, "_get_market_regime", _r)


def no_preview_quotes(monkeypatch):
    # Keeps tests that don't care about the preview-quote branch off the
    # network entirely (real get_preview_quotes would otherwise fire an
    # httpx call for every symbol with no live tick).
    async def _p(symbols):
        return {}
    monkeypatch.setattr(entry, "get_preview_quotes", _p)


# ── lines 458-463: REAL regime-WEAK logging + threshold_age_note ────────────

class TestRegimeWeakLogging:
    def test_regime_weak_logs_with_threshold_age_note(self, db, monkeypatch, caplog):
        # Normal (import-succeeds, call-succeeds) branch: threshold_age_note
        # is real here since adaptive_thresholds.py ships in this repo.
        fake_regime(monkeypatch, ok=False, score=20, threshold=38)
        make_candidate(db, mode="REAL", decision_label="BUY NOW")
        quotes(monkeypatch, {})
        no_preview_quotes(monkeypatch)
        with caplog.at_level(logging.INFO, logger="real-trade-entry"):
            tally = run(entry.evaluate_mode(db, "REAL", gate_armed=True))
        assert "REAL regime WEAK score=20 < gate=38" in caplog.text
        # No live tick was supplied, so nothing entered -- this test's only
        # concern is that the regime-weak log line itself fired without error.
        assert tally["entered"] == 0

    def test_regime_weak_threshold_age_note_exception_falls_back_to_empty_note(
        self, db, monkeypatch, caplog,
    ):
        # Force the `except Exception:` fallback (age_note = "") by making
        # threshold_age_note itself raise when called.
        import adaptive_thresholds

        def _boom(*a, **kw):
            raise RuntimeError("no threshold history yet")

        monkeypatch.setattr(adaptive_thresholds, "threshold_age_note", _boom)
        fake_regime(monkeypatch, ok=False, score=15, threshold=38)
        make_candidate(db, mode="REAL", decision_label="BUY NOW")
        quotes(monkeypatch, {})
        no_preview_quotes(monkeypatch)
        with caplog.at_level(logging.INFO, logger="real-trade-entry"):
            tally = run(entry.evaluate_mode(db, "REAL", gate_armed=True))
        # Must not raise despite threshold_age_note blowing up, and the log
        # line still fires (with an empty age_note rather than a real one).
        assert "REAL regime WEAK score=15 < gate=38" in caplog.text
        assert tally["entered"] == 0


# ── lines 480-482: preview-quote-fetch exception is swallowed ───────────────

class TestPreviewQuoteFetchException:
    def test_preview_fetch_exception_is_swallowed_and_cycle_continues(self, db, monkeypatch, caplog):
        make_candidate(db, decision_label="BUY NOW")
        quotes(monkeypatch, {})  # no live tick -> symbol lands in missing_syms

        async def _boom(symbols):
            raise RuntimeError("market-data-service unreachable")

        monkeypatch.setattr(entry, "get_preview_quotes", _boom)
        with caplog.at_level(logging.DEBUG, logger="real-trade-entry"):
            tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))  # must not raise
        assert "preview quote fetch failed (non-fatal)" in caplog.text
        assert tally["evaluated"] == 1


# ── lines 536-537: pstat.set_symbol_progress exception is swallowed ─────────

class TestSymbolProgressException:
    def test_progress_update_exception_is_swallowed_and_candidate_still_processed(
        self, db, monkeypatch,
    ):
        def _boom(mode, symbol, idx, total):
            raise RuntimeError("progress store unavailable")

        monkeypatch.setattr(entry.pstat, "set_symbol_progress", _boom)
        make_candidate(db, decision_label="BUY NOW")
        quotes(monkeypatch, {"TESTCO": tick(100.0)})
        approve_all(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))  # must not raise
        assert tally["evaluated"] == 1
