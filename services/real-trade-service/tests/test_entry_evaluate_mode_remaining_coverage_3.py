"""
tests/test_entry_evaluate_mode_remaining_coverage_3.py

Final coverage round for entry_engine/entry.py — closes the last 5 missed lines:

  * lines 47-49  : module-level ``except ImportError`` fallback for
                   ``from return_sanity import clamp_for_atr as _clamp_for_atr``.
                   The normal (import-succeeds) branch is exercised by every other
                   test that imports entry_engine.entry; only the fallback function
                   itself had zero direct coverage.  We reload the module under a
                   patched ``builtins.__import__`` (same technique as
                   test_exit_clamp_for_atr_importerror_fallback.py) and then
                   restore the real import in a ``finally`` block.

  * lines 684-685 : ``except Exception: age_note = ""`` inside Gate 3's per-candidate
                   regime-weak WAIT block.  This is a second occurrence of the same
                   try/except pattern that already appears at lines 459-463 (pre-loop
                   logging path, covered by test_entry_evaluate_mode_remaining_coverage.py).
                   The difference here is that a live tick IS present so the candidate
                   enters the per-candidate loop before hitting Gate 3.  We supply a
                   real tick, force ``regime_ok=False``, and make
                   ``adaptive_thresholds.threshold_age_note`` raise so the except-branch
                   fires.

Run from services/real-trade-service:
    python3 -m pytest tests/test_entry_evaluate_mode_remaining_coverage_3.py -q \\
        --cov=entry_engine.entry --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import builtins
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from entry_engine import entry
from market_feed.feed import Tick
from datetime import datetime, timezone


def run(coro):
    return asyncio.run(coro)


# ── shared fixtures ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def pin(monkeypatch):
    """Pin every config knob used by evaluate_mode so tests are deterministic."""
    for k, v in dict(
        ENTRY_MIN_REWARD_RISK=2.0,
        ENTRY_COMPOSITE_WEIGHT_CONVICTION=0.65,
        ENTRY_COMPOSITE_WEIGHT_RR=0.10,
        ENTRY_COMPOSITE_WEIGHT_DRIFT=0.25,
        ENTRY_COMPOSITE_RR_CEILING=4.0,
        MIN_TRADE_VALUE=3000.0,
        MIN_EDGE_TO_COST_RATIO=3.0,
        API_GATEWAY_URL="http://gw",
        COST_MODEL_ENABLED=False,
        ENTRY_CYCLE_QUALITY_FILTER_ENABLED=False,
        ENTRY_MAX_NEW_PER_CYCLE=3,
        ENTRY_MIN_COMPOSITE_SCORE=50.0,
        ENTRY_VALIDITY_MINUTES=15,
        ENTRY_ORDER_TYPE="LIMIT",
        ENTRY_ZONE_UPPER_PCT=0.1,
        ENTRY_REGIME_OVERRIDE_TOP_N=0,
        ENTRY_REGIME_OVERRIDE_RISK_SCALE=0.5,
        ENTRY_OVERNIGHT_PRIORITY_BONUS=10.0,
    ).items():
        monkeypatch.setattr(config, k, v)
    for k, v in dict(
        MIN_REWARD_RISK_RATIO=2.0,
        MAX_ENTRY_DRIFT_ATR=0.75,
        CONVICTION_MIDPOINT=65.0,
        CONVICTION_MAX_SCALE=0.25,
        REGIME_MIN_SCORE_STATIC=38,
    ).items():
        monkeypatch.setattr(entry, k, v)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    s.add(models.TradeAccount(mode="DEMO", starting_capital=100_000.0,
                               current_equity=100_000.0, cash_available=100_000.0))
    s.add(models.TradeAccount(mode="REAL", starting_capital=100_000.0,
                               current_equity=100_000.0, cash_available=100_000.0))
    s.add(models.TradeRiskConfig(mode="DEMO"))
    s.add(models.TradeRiskConfig(mode="REAL"))
    s.commit()
    yield s
    s.close()


def make_candidate(db, *, symbol="TESTCO", decision_label="BUY NOW", conviction=70.0,
                    signal_price=100.0, mode="REAL"):
    cand = models.TradeCandidate(
        mode=mode, symbol=symbol, source_tab="hot_picks",
        decision_label=decision_label, conviction_score=conviction,
        signal_price=signal_price, raw_payload=None, consumed=False,
    )
    db.add(cand)
    db.commit()
    db.refresh(cand)
    return cand


def _tick(price=100.0, atr=2.0, symbol="TESTCO"):
    return Tick(symbol=symbol, price=price, as_of=datetime.now(timezone.utc),
                atr=atr, source="test", volume=None)


def _quotes(monkeypatch, mapping: dict):
    async def _q(symbols):
        return dict(mapping)
    monkeypatch.setattr(entry, "get_quotes", _q)


def _fake_regime(monkeypatch, *, ok, score=20, threshold=38, src="adaptive"):
    async def _r(db):
        return (ok, score, threshold, src)
    monkeypatch.setattr(entry, "_get_market_regime", _r)


# ═══════════════════════════════════════════════════════════════════════════════
# 1.  lines 47-49 — module-level return_sanity ImportError fallback
# ═══════════════════════════════════════════════════════════════════════════════

class TestEntryReturnSanityImportErrorFallback:
    """
    Cover the ``except ImportError: def _clamp_for_atr(x): ...`` block
    (lines 47-49) by reloading entry_engine.entry under a patched
    builtins.__import__ that blocks 'return_sanity'.
    """

    def test_normal_import_uses_return_sanity_clamp(self):
        """Sanity-check: the real return_sanity is present, so the normal
        branch was taken at import time and _clamp_for_atr is the real one."""
        import return_sanity
        assert entry._clamp_for_atr is return_sanity.clamp_for_atr

    def test_importerror_fallback_defines_inline_clamp(self):
        """
        When return_sanity is missing (ImportError), the fallback inline
        function is used.  Reload under a patched __import__, verify the
        fallback is NOT the real return_sanity function, exercise its contract,
        then restore the real module so subsequent tests are unaffected.
        """
        import entry_engine.entry as ee_mod

        real_import = builtins.__import__

        def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "return_sanity":
                raise ImportError("simulated: return_sanity unavailable")
            return real_import(name, globals, locals, fromlist, level)

        builtins.__import__ = _fake_import
        try:
            reloaded = importlib.reload(ee_mod)
        finally:
            builtins.__import__ = real_import

        try:
            import return_sanity
            # The fallback must be the locally-defined lambda, not the real one.
            assert reloaded._clamp_for_atr is not return_sanity.clamp_for_atr

            # Contract: ``None if x is None or abs(x) > 30.0 else x``
            assert reloaded._clamp_for_atr(None) is None         # None → None
            assert reloaded._clamp_for_atr(5.0) == 5.0           # normal value kept
            assert reloaded._clamp_for_atr(-5.0) == -5.0         # negative kept
            assert reloaded._clamp_for_atr(30.0) == 30.0         # boundary: ≤ 30, kept
            assert reloaded._clamp_for_atr(30.1) is None          # just past boundary
            assert reloaded._clamp_for_atr(-31.0) is None         # negative side
            assert reloaded._clamp_for_atr(999.0) is None         # obvious corporate jump
        finally:
            # Restore production import so every other test in the session is clean.
            importlib.reload(ee_mod)
            import return_sanity as _rs
            assert entry._clamp_for_atr is _rs.clamp_for_atr


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  lines 684-685 — Gate 3 except Exception: age_note = "" (per-candidate loop)
# ═══════════════════════════════════════════════════════════════════════════════

class TestGate3RegimeWeakAgeNoteExceptionFallback:
    """
    Cover the ``except Exception: age_note = ""`` branch at lines 684-685
    inside Gate 3's per-candidate block.

    The existing test_entry_evaluate_mode_remaining_coverage.py already
    covers the identical pattern at lines 459-463 (pre-loop logging), but
    that test supplies no live tick so it never reaches the per-candidate
    loop.  Here we supply a real tick so the candidate IS processed, then
    make ``adaptive_thresholds.threshold_age_note`` raise, forcing the
    except-branch.
    """

    def test_age_note_exception_falls_back_silently_and_still_waits(
        self, db, monkeypatch
    ):
        """
        When threshold_age_note() raises inside Gate 3's try/except, the
        except-branch sets age_note = "" and processing continues — the
        candidate still results in a WAIT with the regime-weak reasoning.
        """
        import adaptive_thresholds

        def _boom(*args, **kwargs):
            raise RuntimeError("threshold store unavailable")

        monkeypatch.setattr(adaptive_thresholds, "threshold_age_note", _boom)

        # regime_ok=False → Gate 3 fires for every REAL candidate.
        _fake_regime(monkeypatch, ok=False, score=18, threshold=38)

        # Supply a live tick so the candidate enters the per-candidate loop.
        make_candidate(db, mode="REAL", decision_label="BUY NOW", conviction=75.0)
        _quotes(monkeypatch, {"TESTCO": _tick(100.0)})

        tally = run(entry.evaluate_mode(db, "REAL", gate_armed=True))

        # The cycle must have produced a WAIT, not raised.
        assert tally["waited"] == 1
        assert tally["entered"] == 0

        detail = tally["entry_details"][0]
        assert detail["action"] == "WAIT"
        # The regime-weak reasoning string should still appear even with an
        # empty age_note (the format string just omits the annotation).
        assert "Nifty regime is weak" in detail["reasoning"]

    def test_age_note_exception_does_not_raise_even_with_multiple_candidates(
        self, db, monkeypatch
    ):
        """
        Same scenario with two candidates — both should WAIT cleanly even
        though threshold_age_note raises on every call.
        """
        import adaptive_thresholds

        call_count = {"n": 0}

        def _boom(*args, **kwargs):
            call_count["n"] += 1
            raise ValueError("injected failure #{}".format(call_count["n"]))

        monkeypatch.setattr(adaptive_thresholds, "threshold_age_note", _boom)
        _fake_regime(monkeypatch, ok=False, score=10, threshold=38)

        make_candidate(db, mode="REAL", symbol="AAA", decision_label="BUY NOW")
        make_candidate(db, mode="REAL", symbol="BBB", decision_label="BUY NOW")
        _quotes(monkeypatch, {
            "AAA": _tick(200.0, symbol="AAA"),
            "BBB": _tick(300.0, symbol="BBB"),
        })

        tally = run(entry.evaluate_mode(db, "REAL", gate_armed=True))

        assert tally["waited"] == 2
        assert tally["entered"] == 0
        # threshold_age_note was called once per candidate (both raised).
        assert call_count["n"] == 2
