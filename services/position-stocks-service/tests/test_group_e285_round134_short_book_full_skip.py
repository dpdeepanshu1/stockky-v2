"""E285 round 134: the short cycle skips its quality-gate calls and attempts while no slot is free (every open position against MAX_CONCURRENT_SCALP_POSITIONS,
open shorts against the short cap), and counts the skipped pass once instead of one SHORT_MAX_POSITIONS refusal per shortlisted name. Real models on in-memory SQLite."""
import asyncio
import types
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import config
import models
from orders import short_entry as se, short_live

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 3, 10, 30, tzinfo=IST)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    se.reset()
    monkeypatch.setattr(config, "SHORT_MODE", "live", raising=False)
    monkeypatch.setattr(config, "ENTRY_PRECHECK", True, raising=False)
    monkeypatch.setattr(config, "MAX_CONCURRENT_SCALP_POSITIONS", 5, raising=False)
    monkeypatch.setattr(config, "SHORT_MAX_POSITIONS", 2, raising=False)
    monkeypatch.setattr(short_live, "_short_cap", lambda db, sid: 2)
    yield s
    s.close()


def _pos(db, sym, side="LONG", status="OPEN"):
    db.add(models.ScalpPosition(symbol=sym, dhan_security_id="1", window_source="5m", status=status, side=side, entry_price=100.0, signal_price=100.0, quantity=1,
                                target_price=102.0, stop_price=99.0, adaptive_target_pct=2.0, adaptive_stop_pct=1.0, capital_risked=100.0))
    db.commit()


def test_free_slots_do_not_skip(db):
    _pos(db, "A")
    _pos(db, "B", side="SHORT")
    assert short_live.book_full(db) is None


def test_whole_book_full_skips_with_the_count(db):
    for i, side in enumerate(("LONG", "LONG", "LONG", "SHORT", "LONG")):
        _pos(db, f"S{i}", side=side)
    assert short_live.book_full(db) == "MAX_POSITIONS:5"


def test_short_cap_reached_skips_even_with_free_book_slots(db):
    _pos(db, "A", side="SHORT")
    _pos(db, "B", side="SHORT")
    assert short_live.book_full(db) == "2 live shorts open (max 2)"


def test_closed_positions_do_not_count_and_exit_legs_rejected_does(db):
    _pos(db, "A", side="SHORT", status="TARGET_HIT")
    _pos(db, "B", side="SHORT", status="STOP_HIT")
    assert short_live.book_full(db) is None
    _pos(db, "C", side="SHORT", status="EXIT_LEGS_REJECTED")
    _pos(db, "D", side="SHORT")
    assert short_live.book_full(db) is not None


def test_precheck_off_never_skips_and_errors_fail_open(db, monkeypatch):
    for i in range(5):
        _pos(db, f"S{i}")
    monkeypatch.setattr(config, "ENTRY_PRECHECK", False)
    assert short_live.book_full(db) is None
    monkeypatch.setattr(config, "ENTRY_PRECHECK", True)
    monkeypatch.setattr(short_live, "_open_counts", lambda d: (_ for _ in ()).throw(RuntimeError("db down")))
    assert short_live.book_full(db) is None


def test_the_cycle_skips_the_gate_calls_and_attempts_when_full_and_counts_once(db, monkeypatch):
    import screening.quality_gate as qg
    from screening import engine
    import tz_utils
    for i in range(5):
        _pos(db, f"S{i}")
    monkeypatch.setattr(tz_utils, "is_market_open_ist", lambda *a, **k: True)
    cands = [types.SimpleNamespace(symbol=s, window_label="5m", pct_change=-1.5, composite_score=3.0, current_ltp=100.0) for s in ("A", "B", "C")]
    monkeypatch.setattr(engine, "scan_shorts", lambda held=None: cands)
    monkeypatch.setattr(se, "_bear_rows", lambda: {})
    monkeypatch.setattr(short_live, "automatic_entry_off", lambda d: None)
    called = []

    async def _check(sym, cached=None):
        called.append(sym)
    monkeypatch.setattr(qg, "check", _check)
    monkeypatch.setattr(short_live, "attempt_short_live", lambda *a, **k: called.append("attempt"))
    for _ in range(3):
        out = asyncio.run(se.run_short_cycle(db, NOW))
        assert out["skipped"] == "SHORT_BOOK_FULL" and out["recorded"] is None
    assert called == []
    g = se.summary()["gate_counters"]
    assert g["book_full_skips"] == 3 and g["last_book_full"] == "MAX_POSITIONS:5" and g["refused"] == 0 and g["by_code"] == {} and g["scanned"] == 9


def test_the_cycle_runs_normally_when_a_slot_is_free(db, monkeypatch):
    import screening.quality_gate as qg
    from screening import engine
    import tz_utils
    _pos(db, "S0")
    monkeypatch.setattr(tz_utils, "is_market_open_ist", lambda *a, **k: True)
    monkeypatch.setattr(engine, "scan_shorts", lambda held=None: [])
    monkeypatch.setattr(se, "_bear_rows", lambda: {})
    monkeypatch.setattr(short_live, "automatic_entry_off", lambda d: None)
    monkeypatch.setattr(qg, "get_cache_batch", lambda *a, **k: {})
    out = asyncio.run(se.run_short_cycle(db, NOW))
    assert out.get("skipped") is None and out["recorded"] == 0
    assert se.summary()["gate_counters"]["book_full_skips"] == 0


def test_reset_clears_the_new_counters_and_the_skip_sits_after_the_recorded_scan():
    se._stats["book_full_skips"], se._stats["last_book_full"] = 4, "x"
    se.reset()
    assert se._stats["book_full_skips"] == 0 and se._stats["last_book_full"] is None
    src = (ROOT / "orders" / "short_entry.py").read_text(encoding="utf-8")
    assert src.index('_scan_hits.record_scan(cands, side="short")') < src.index("_sl_full.book_full") < src.index("quality_gate.check(c.symbol")
