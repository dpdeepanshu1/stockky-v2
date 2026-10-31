"""Short selling step 5: orders/short_entry.py in SHADOW. Real models on in-memory SQLite. No order is ever placed: the broker module is not even imported."""
import asyncio
import types
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import config
import models
from capital import ledger
from orders import short_entry as se, short_gates

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 23, 10, 30, tzinfo=IST)
AFTER_SQ = datetime(2026, 10, 23, 15, 0, tzinfo=IST)


@pytest.fixture()
def db(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    row = ledger._get_or_create(s)
    row.total_allocated_capital, row.available_capital = 100000.0, 100000.0
    s.commit()
    se.reset()
    monkeypatch.setattr(config, "SHORT_MODE", "shadow", raising=False)
    monkeypatch.setattr(short_gates, "gather_facts", lambda *a, **k: {"live": 100.0})
    monkeypatch.setattr(short_gates, "evaluate", lambda f, now=None: None)
    for mod, name in ((se.entry_pause, "all_paused"), (se.stopout_guard, "entry_reason"), (se.calendar_gate, "entry_block_reason"),
                      (se.calendar_gate, "scalp_cutoff_reason"), (se.depth_gate, "max_qty_from_book"), (se.depth_gate, "max_qty_from_depth20"),
                      (se.cost_gate, "reject_reason"), (se.entry_pause, "symbol_blocked")):
        monkeypatch.setattr(mod, name, lambda *a, **k: None)
    monkeypatch.setattr(se.flow_gate, "entry_check", lambda *a, **k: (None, None))      # E269: no order-flow tape (and no network call) unless a test sets one
    from screening import opening_gate
    monkeypatch.setattr(opening_gate, "stop_reject", lambda *a, **k: None)
    lv = types.SimpleNamespace(stop_price=101.0, target_price=98.0, stop_pct=1.0, target_pct=2.0, side="SHORT")
    monkeypatch.setattr(se, "compute_levels", lambda *a, **k: lv)
    yield s
    s.close()


def cand(sym="X"):
    return types.SimpleNamespace(symbol=sym, window_label="5m", pct_change=-1.5, composite_score=3.0, current_ltp=100.0)


def test_off_mode_records_nothing(db, monkeypatch):
    monkeypatch.setattr(config, "SHORT_MODE", "off")
    assert se.attempt_short_shadow(db, cand(), None, {"conviction": 70}, NOW) is None
    assert db.query(models.ScalpShortShadow).count() == 0 and db.query(models.ScalpCandidateLog).count() == 0


def test_a_live_mode_value_also_reads_as_off(db, monkeypatch):
    monkeypatch.setattr(config, "SHORT_MODE", "live")
    assert se.mode() == "off"


def test_a_clean_candidate_is_recorded_sized_and_logged(db):
    r = se.attempt_short_shadow(db, cand(), None, {"conviction": 70}, NOW)
    assert r is not None and r.status == "OPEN" and r.entry_price == 100.0 and r.quantity == 200      # 100000 x 2% x 0.5 / 5 slots / 1% stop
    assert r.target_price < r.entry_price < r.stop_price
    log = db.query(models.ScalpCandidateLog).one()
    assert log.decision == "SHORT_SHADOW" and "no order" in log.reason
    assert db.query(models.ScalpPosition).count() == 0                                                  # no position row, no capital reserved
    assert ledger._get_or_create(db).available_capital == 100000.0


def test_one_shadow_per_symbol_per_day_and_the_max_positions_cap(db):
    assert se.attempt_short_shadow(db, cand("A"), None, None, NOW) is not None
    assert se.attempt_short_shadow(db, cand("A"), None, None, NOW) is None
    assert se.attempt_short_shadow(db, cand("B"), None, None, NOW) is not None
    assert se.attempt_short_shadow(db, cand("C"), None, None, NOW) is None                              # SHORT_MAX_POSITIONS = 2
    assert db.query(models.ScalpShortShadow).count() == 2


def test_a_gate_refusal_logs_one_skip_row_per_five_minutes(db, monkeypatch):
    monkeypatch.setattr(short_gates, "evaluate", lambda f, now=None: ("VWAP_ABOVE", "buyers in control"))
    assert se.attempt_short_shadow(db, cand(), None, None, NOW) is None
    assert se.attempt_short_shadow(db, cand(), None, None, NOW) is None
    rows = db.query(models.ScalpCandidateLog).all()
    assert len(rows) == 1 and rows[0].decision == "SHORT_SKIP" and rows[0].reason.startswith("VWAP_ABOVE:")
    assert db.query(models.ScalpShortShadow).count() == 0
    assert se.summary(db)["gate_counters"]["by_code"]["VWAP_ABOVE"] == 2


def test_e269_a_reliable_adverse_tape_refuses_the_shadow_short(db, monkeypatch):
    seen = []
    monkeypatch.setattr(se.flow_gate, "entry_check", lambda sym, side, fj=None: (seen.append((sym, side)) or ("ORDER_FLOW:3_of_6_against_short(delta)", None)))
    assert se.attempt_short_shadow(db, cand(), None, {"conviction": 70}, NOW) is None
    assert seen == [("X", "SHORT")]                                                     # the SHORT checklist, not the long one
    rows = db.query(models.ScalpCandidateLog).all()
    assert len(rows) == 1 and rows[0].decision == "SHORT_SKIP" and rows[0].reason.startswith("SHORT_ORDER_FLOW:")
    assert db.query(models.ScalpShortShadow).count() == 0
    assert se.summary(db)["gate_counters"]["by_code"]["SHORT_ORDER_FLOW"] == 1


def test_e269_no_tape_opinion_leaves_the_shadow_short_untouched_and_plan_carries_the_tape(db, monkeypatch):
    monkeypatch.setattr(se.flow_gate, "entry_check", lambda sym, side, fj=None: (None, '{"tape":{"against":1}}'))
    plan = se.plan_short(db, cand(), None, {"conviction": 70}, NOW)
    assert plan is not None and plan["tape_json"] == '{"tape":{"against":1}}'
    assert se.attempt_short_shadow(db, cand("Y"), None, {"conviction": 70}, NOW) is not None


def test_a_symbol_held_as_a_long_is_refused(db, monkeypatch):
    class _Q:
        def __init__(self, hit): self.hit = hit
        def filter(self, *a): return self
        def first(self): return self.hit
        def all(self): return []
    real = db.query
    monkeypatch.setattr(db, "query", lambda m, *a: _Q(object()) if m is models.ScalpPosition else real(m, *a))
    assert se.attempt_short_shadow(db, cand(), None, None, NOW) is None
    assert real(models.ScalpShortShadow).count() == 0


def test_no_capital_is_a_refusal_not_a_guess(db):
    row = ledger._get_or_create(db)
    row.available_capital = 1.0
    db.commit()
    assert se.attempt_short_shadow(db, cand(), None, None, NOW) is None
    assert db.query(models.ScalpCandidateLog).one().reason.startswith("SHORT_NO_CAPITAL")


def _open_row(db, **kw):
    d = dict(day="2026-10-23", symbol="X", entry_price=100.0, stop_price=101.0, target_price=98.0, quantity=10, status="OPEN", low_water=100.0, high_water=100.0)
    d.update(kw)
    r = models.ScalpShortShadow(**d)
    db.add(r)
    db.commit()
    return r


@pytest.mark.parametrize("px,status,exit_px,gross", [(101.5, "STOP", 101.5, -15.0), (97.9, "TARGET", 98.0, 20.0)])
def test_resolve_stop_and_target(db, monkeypatch, px, status, exit_px, gross):
    r = _open_row(db)
    monkeypatch.setattr(se, "_live_price", lambda s: px)
    assert se.resolve_open(db, NOW) == 1
    db.refresh(r)
    assert r.status == status and r.exit_price == exit_px and r.gross_pnl == gross and r.net_pnl < gross


def test_resolve_leaves_a_midrange_row_open_and_tracks_water(db, monkeypatch):
    r = _open_row(db)
    monkeypatch.setattr(se, "_live_price", lambda s: 99.5)
    assert se.resolve_open(db, NOW) == 0
    db.refresh(r)
    assert r.status == "OPEN" and r.low_water == 99.5


def test_resolve_closes_at_the_short_squareoff_time(db, monkeypatch):
    r = _open_row(db)
    monkeypatch.setattr(se, "_live_price", lambda s: 99.5)
    assert se.resolve_open(db, AFTER_SQ) == 1
    db.refresh(r)
    assert r.status == "EOD" and r.exit_price == 99.5 and r.gross_pnl == 5.0


def test_a_missing_price_leaves_the_row_open(db, monkeypatch):
    r = _open_row(db)
    monkeypatch.setattr(se, "_live_price", lambda s: None)
    assert se.resolve_open(db, NOW) == 0
    db.refresh(r)
    assert r.status == "OPEN"


def test_stop_wins_when_both_levels_are_crossed(db, monkeypatch):
    r = _open_row(db, stop_price=99.0, target_price=101.0)           # nonsense geometry: price 100 is past both -> the conservative STOP
    monkeypatch.setattr(se, "_live_price", lambda s: 100.0)
    se.resolve_open(db, NOW)
    db.refresh(r)
    assert r.status == "STOP"


def test_an_unresolved_row_from_an_earlier_day_expires_without_a_result(db, monkeypatch):
    r = _open_row(db, day="2026-10-22")
    monkeypatch.setattr(se, "_live_price", lambda s: 99.5)
    assert se.resolve_open(db, NOW) == 1
    db.refresh(r)
    assert r.status == "EXPIRED" and r.net_pnl is None


def test_summary_counts_closed_rows_only(db, monkeypatch):
    _open_row(db, symbol="A")
    _open_row(db, symbol="B")
    monkeypatch.setattr(se, "_live_price", lambda s: 97.0 if s == "A" else 99.0)
    se.resolve_open(db, NOW)
    s = se.summary(db)
    assert s["total"] == 2 and s["closed"] == 1 and s["open"] == 1 and s["target"] == 1 and s["net_pnl"] > 0


def test_cycle_in_off_mode_places_nothing_and_does_not_scan(db, monkeypatch):
    monkeypatch.setattr(config, "SHORT_MODE", "off")
    from screening import engine
    monkeypatch.setattr(engine, "scan_shorts", lambda *a, **k: (_ for _ in ()).throw(AssertionError("scanned in off mode")))
    out = asyncio.run(se.run_short_cycle(db, NOW))
    assert out["mode"] == "off" and out["recorded"] is None


def test_cycle_in_off_mode_still_closes_leftover_rows(db, monkeypatch):
    r = _open_row(db)
    monkeypatch.setattr(config, "SHORT_MODE", "off")
    monkeypatch.setattr(se, "_live_price", lambda s: 101.5)
    assert asyncio.run(se.run_short_cycle(db, NOW))["resolved"] == 1
    db.refresh(r)
    assert r.status == "STOP"


def test_shadow_cycle_records_a_faller_that_the_board_also_ranks_bearish(db, monkeypatch):
    import screening.quality_gate as qg
    from screening import engine
    from tz_utils import is_market_open_ist  # noqa: F401
    import tz_utils
    monkeypatch.setattr(tz_utils, "is_market_open_ist", lambda *a, **k: True)
    monkeypatch.setattr(engine, "scan_shorts", lambda held=None: [cand("A"), cand("NOBOARD")])
    monkeypatch.setattr(se, "_bear_rows", lambda: {"A": {"side": "bear", "conviction": 70, "symbol": "A"}})

    async def _check(sym, cached=None):
        return types.SimpleNamespace(event_unchecked=False, has_positive_catalyst=False, bulk_deal_flag=False, earnings_date=None)
    monkeypatch.setattr(qg, "check", _check)
    monkeypatch.setattr(qg, "get_cache_batch", lambda *a, **k: {})
    out = asyncio.run(se.run_short_cycle(db, NOW))
    assert out["recorded"] == 1
    assert [r.symbol for r in db.query(models.ScalpShortShadow).all()] == ["A"]
