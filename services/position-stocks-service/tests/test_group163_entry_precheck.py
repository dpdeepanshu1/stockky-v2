"""
group163 (item 4): cheap checks BEFORE the quality gate (main.py).

SATIN and COMSYN were quality-gated (up to QUALITY_GATE_TOP_N HTTP calls) on every ~13 s cycle even though
nothing could be entered: the pool's available capital was below one share of them, or every position slot was
already used. Both are exact lower bounds, so a candidate attempt_entry() could have entered is never dropped.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_group163_entry_precheck.py -q
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import config
import models
import main as m
from screening.engine import Candidate
from screening.quality_gate import QualitySignal


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng)()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    m._capital_starved.clear()
    m._unaffordable_logged_at = float("-inf")
    monkeypatch.setattr(config, "ENTRY_PRECHECK", True)
    yield
    m._capital_starved.clear()
    m._unaffordable_logged_at = float("-inf")


def _cand(symbol="SBIN", ltp=100.0):
    return Candidate(symbol=symbol, window_minutes=5, pct_change=2.0, current_ltp=ltp,
                     tick_activity=50, composite_score=10.0)


def _gate(db, **kw):
    row = models.ScalpGateState(mode="REAL", service_enabled=True, is_armed=True, auto_pilot_enabled=True)
    for k, v in kw.items():
        setattr(row, k, v)
    db.add(row)
    db.commit()
    return row


def _position(db, symbol, status="OPEN"):
    row = models.ScalpPosition(
        symbol=symbol, dhan_security_id="1", window_source="5m", status=status, entry_price=100.0, quantity=1,
        target_price=103.0, stop_price=98.0, adaptive_target_pct=3.0, adaptive_stop_pct=2.0, capital_risked=100.0)
    db.add(row)
    db.commit()
    return row


def _avail(monkeypatch, value):
    monkeypatch.setattr(m.ledger, "get_state", lambda _db: {"available_capital": value})


class TestUnaffordableFilter:
    def test_drops_only_candidates_priced_above_available_capital(self, db, monkeypatch):
        _avail(monkeypatch, 150.0)
        kept, dropped = m._unaffordable_filter(db, [_cand("A", 100.0), _cand("SATIN", 151.0), _cand("COMSYN", 900.0)])
        assert [c.symbol for c in kept] == ["A"]
        assert dropped == ["SATIN", "COMSYN"]

    def test_price_equal_to_capital_is_kept(self, db, monkeypatch):
        _avail(monkeypatch, 100.0)
        kept, dropped = m._unaffordable_filter(db, [_cand("EDGE", 100.0)])
        assert [c.symbol for c in kept] == ["EDGE"] and dropped == []

    def test_zero_capital_drops_everyone(self, db, monkeypatch):
        _avail(monkeypatch, 0.0)
        kept, dropped = m._unaffordable_filter(db, [_cand("A", 1.0)])
        assert kept == [] and dropped == ["A"]

    def test_none_capital_is_treated_as_zero(self, db, monkeypatch):
        _avail(monkeypatch, None)
        kept, dropped = m._unaffordable_filter(db, [_cand("A", 1.0)])
        assert kept == [] and dropped == ["A"]

    def test_order_of_kept_candidates_is_preserved(self, db, monkeypatch):
        _avail(monkeypatch, 500.0)
        kept, _ = m._unaffordable_filter(db, [_cand("C", 10), _cand("A", 20), _cand("B", 30)])
        assert [c.symbol for c in kept] == ["C", "A", "B"]

    def test_ledger_failure_is_fail_open(self, db, monkeypatch):
        monkeypatch.setattr(m.ledger, "get_state", lambda _db: (_ for _ in ()).throw(RuntimeError("boom")))
        cands = [_cand("A", 9999.0)]
        kept, dropped = m._unaffordable_filter(db, cands)
        assert kept == cands and dropped == []

    def test_unreadable_price_is_kept(self, db, monkeypatch):
        _avail(monkeypatch, 10.0)
        weird = _cand("W", 5.0)
        weird.current_ltp = None
        kept, dropped = m._unaffordable_filter(db, [weird])
        assert kept == [weird] and dropped == []

    def test_switch_off_keeps_everyone(self, db, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_PRECHECK", False)
        _avail(monkeypatch, 0.0)
        cands = [_cand("A", 500.0)]
        assert m._unaffordable_filter(db, cands) == (cands, [])

    def test_empty_list(self, db, monkeypatch):
        _avail(monkeypatch, 0.0)
        assert m._unaffordable_filter(db, []) == ([], [])


class TestOpenSlotsFull:
    def test_below_cap_returns_none(self, db, monkeypatch):
        monkeypatch.setattr(config, "MAX_CONCURRENT_SCALP_POSITIONS", 2)
        _position(db, "A")
        assert m._open_slots_full(db) is None

    def test_at_cap_returns_count(self, db, monkeypatch):
        monkeypatch.setattr(config, "MAX_CONCURRENT_SCALP_POSITIONS", 2)
        _position(db, "A")
        _position(db, "B")
        assert m._open_slots_full(db) == 2

    def test_exit_legs_rejected_counts_like_attempt_entry(self, db, monkeypatch):
        monkeypatch.setattr(config, "MAX_CONCURRENT_SCALP_POSITIONS", 2)
        _position(db, "A")
        _position(db, "B", status="EXIT_LEGS_REJECTED")
        assert m._open_slots_full(db) == 2

    def test_closed_positions_do_not_count(self, db, monkeypatch):
        monkeypatch.setattr(config, "MAX_CONCURRENT_SCALP_POSITIONS", 1)
        _position(db, "A", status="CLOSED")
        assert m._open_slots_full(db) is None

    def test_db_error_is_fail_open(self, monkeypatch):
        class _Broken:
            def query(self, *a, **k):
                raise RuntimeError("db down")
        assert m._open_slots_full(_Broken()) is None

    def test_switch_off(self, db, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_PRECHECK", False)
        monkeypatch.setattr(config, "MAX_CONCURRENT_SCALP_POSITIONS", 1)
        _position(db, "A")
        assert m._open_slots_full(db) is None


class TestInTheCycle:
    """Drives _run_cycle with the quality gate counted, so we can see it is NOT called."""

    def _setup(self, db, monkeypatch, cands, avail=100000.0):
        async def _no_market_block():
            return None
        monkeypatch.setattr(m.trade_gates, "market_gate_reject", _no_market_block)
        monkeypatch.setattr(m.trade_gates, "loss_brake_reject", lambda db_, now=None: None)
        monkeypatch.setattr(m.reconcile, "run_exit_reconciliation", lambda db_: 0)
        monkeypatch.setattr(m, "ist_time_at_or_after", lambda t: False)
        monkeypatch.setattr(m, "ist_now", lambda: datetime(2026, 9, 25, 11, 0, tzinfo=m.IST))
        monkeypatch.setattr(m.ledger, "sync_from_broker", lambda db_: 100000.0)
        _avail(monkeypatch, avail)
        monkeypatch.setattr(m.intraday_eligibility, "get_restricted_symbols", lambda db_: set())
        monkeypatch.setattr(m, "scan", lambda **kw: cands)
        calls = {"cache": 0, "check": []}
        monkeypatch.setattr(m.quality_gate, "get_cache_batch",
                            lambda db_, syms: calls.__setitem__("cache", calls["cache"] + 1) or {})
        monkeypatch.setattr(m.quality_gate, "upsert_cache_batch", lambda db_, res: None)

        async def _check(symbol, cached=None):
            calls["check"].append(symbol)
            return QualitySignal(symbol=symbol, fundamental_score=10.0, technical_score=10.0, market_cap_cr=1.0)
        monkeypatch.setattr(m.quality_gate, "check", _check)
        monkeypatch.setattr(m, "log_quality_reject", lambda db_, c, q, r: None)
        _gate(db)
        return calls

    def test_full_slots_skip_quality_gate_and_market_filter(self, db, monkeypatch):
        monkeypatch.setattr(config, "MAX_CONCURRENT_SCALP_POSITIONS", 1)
        calls = self._setup(db, monkeypatch, [_cand("SATIN", 150.0)])
        _position(db, "HELD")
        fetched = {"n": 0}

        async def _mkt():
            fetched["n"] += 1
            return None
        monkeypatch.setattr(m.trade_gates, "market_gate_reject", _mkt)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert summary["skipped_reason"] == "MAX_POSITIONS_FULL"
        assert calls["cache"] == 0 and calls["check"] == [] and fetched["n"] == 0
        stage = next(s for s in summary["stages"] if s["name"] == "quality_gate")
        assert "1/1" in stage["detail"]

    def test_full_slots_still_screen_so_candidates_stay_visible(self, db, monkeypatch):
        monkeypatch.setattr(config, "MAX_CONCURRENT_SCALP_POSITIONS", 1)
        self._setup(db, monkeypatch, [_cand("SATIN", 150.0), _cand("COMSYN", 200.0)])
        _position(db, "HELD")
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert summary["candidates_seen"] == 2

    def test_switch_off_runs_quality_gate_even_when_full(self, db, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_PRECHECK", False)
        monkeypatch.setattr(config, "MAX_CONCURRENT_SCALP_POSITIONS", 1)
        calls = self._setup(db, monkeypatch, [_cand("SATIN", 150.0)])
        _position(db, "HELD")
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert summary["skipped_reason"] != "MAX_POSITIONS_FULL"
        assert calls["check"] == ["SATIN"]

    def test_auto_pilot_off_still_wins_over_full_slots(self, db, monkeypatch):
        monkeypatch.setattr(config, "MAX_CONCURRENT_SCALP_POSITIONS", 1)
        calls = self._setup(db, monkeypatch, [_cand("SATIN", 150.0)])
        _position(db, "HELD")
        db.query(models.ScalpGateState).update({"auto_pilot_enabled": False})
        db.commit()
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert summary["skipped_reason"] == "AUTO_PILOT_OFF"
        assert calls["check"] == []

    def test_all_unaffordable_skips_quality_gate(self, db, monkeypatch):
        calls = self._setup(db, monkeypatch, [_cand("SATIN", 150.0), _cand("COMSYN", 900.0)], avail=120.0)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert summary["skipped_reason"] == "ALL_CANDIDATES_UNAFFORDABLE"
        assert summary["unaffordable_skipped"] == ["SATIN", "COMSYN"]
        assert calls["cache"] == 0 and calls["check"] == []

    def test_only_affordable_candidates_are_quality_gated(self, db, monkeypatch):
        calls = self._setup(db, monkeypatch,
                            [_cand("SATIN", 150.0), _cand("CHEAP", 50.0), _cand("COMSYN", 900.0)], avail=120.0)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert calls["check"] == ["CHEAP"]
        assert summary["unaffordable_skipped"] == ["SATIN", "COMSYN"]

    def test_top_n_slot_goes_to_next_affordable_candidate(self, db, monkeypatch):
        monkeypatch.setattr(config, "QUALITY_GATE_TOP_N", 2)
        monkeypatch.setattr(config, "MIN_PREFERRED_SCALP_POSITIONS", 0)   # not "under preferred": no extra top-N slots
        cands = [_cand("SATIN", 150.0), _cand("COMSYN", 900.0), _cand("A", 10.0), _cand("B", 20.0), _cand("C", 30.0)]
        calls = self._setup(db, monkeypatch, cands, avail=120.0)
        asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert calls["check"] == ["A", "B"]

    def test_unaffordable_log_is_rate_limited(self, db, monkeypatch, caplog):
        calls = self._setup(db, monkeypatch, [_cand("SATIN", 150.0), _cand("OK", 10.0)], avail=120.0)
        with caplog.at_level(logging.INFO):
            asyncio.run(m._run_cycle(db, trigger="AUTO"))
            asyncio.run(m._run_cycle(db, trigger="AUTO"))
        lines = [r for r in caplog.records if "priced above available capital" in r.getMessage()]
        assert len(lines) == 1 and "SATIN" in lines[0].getMessage()
        assert calls["check"] == ["OK", "OK"]

    def test_nothing_unaffordable_logs_nothing(self, db, monkeypatch, caplog):
        self._setup(db, monkeypatch, [_cand("OK", 10.0)], avail=120.0)
        with caplog.at_level(logging.INFO):
            summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert summary["unaffordable_skipped"] == []
        assert not [r for r in caplog.records if "priced above available capital" in r.getMessage()]

    def test_switch_off_quality_gates_unaffordable_candidates(self, db, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_PRECHECK", False)
        calls = self._setup(db, monkeypatch, [_cand("SATIN", 150.0)], avail=120.0)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert summary["skipped_reason"] != "ALL_CANDIDATES_UNAFFORDABLE"
        assert calls["check"] == ["SATIN"]
