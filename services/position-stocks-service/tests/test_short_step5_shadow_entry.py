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


@pytest.fixture()
def db(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    row = ledger._get_or_create(s)
    row.total_allocated_capital, row.available_capital = 100000.0, 100000.0
    s.commit()
    se.reset()
    monkeypatch.setattr(config, "SHORT_MODE", "live", raising=False)
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


def _plan(db, c, q=None, brow=None, now=None):
    return se.plan_short(db, c, q, brow, now or NOW)


def test_cfg_mode_has_no_shadow_value():
    for raw, want in (("live", "live"), (" LIVE ", "live"), ("off", "off"), ("shadow", "off"), (" SHADOW ", "off"), ("junk", "off"), (None, "off")):
        config.SHORT_MODE = raw
        assert se.cfg_mode() == want, raw
    config.SHORT_MODE = "live"


def test_a_clean_candidate_is_planned_and_nothing_is_recorded_or_reserved(db):
    r = _plan(db, cand(), None, {"conviction": 70})
    assert r is not None and r["qty"] == 200 and r["live"] == 100.0                                  # 100000 x 2% x 0.5 / 5 slots / 1% stop
    assert r["levels"].target_price < r["live"] < r["levels"].stop_price
    assert db.query(models.ScalpShortShadow).count() == 0 and db.query(models.ScalpPosition).count() == 0
    assert ledger._get_or_create(db).available_capital == 100000.0


def test_a_gate_refusal_logs_one_skip_row_per_five_minutes(db, monkeypatch):
    monkeypatch.setattr(short_gates, "evaluate", lambda f, now=None: ("VWAP_ABOVE", "buyers in control"))
    assert _plan(db, cand()) is None
    assert _plan(db, cand()) is None
    rows = db.query(models.ScalpCandidateLog).all()
    assert len(rows) == 1 and rows[0].decision == "SHORT_SKIP" and rows[0].reason.startswith("VWAP_ABOVE:")
    assert db.query(models.ScalpShortShadow).count() == 0
    assert se.summary(db)["gate_counters"]["by_code"]["VWAP_ABOVE"] == 2


def test_e269_a_reliable_adverse_tape_refuses_the_shadow_short(db, monkeypatch):
    seen = []
    monkeypatch.setattr(se.flow_gate, "entry_check", lambda sym, side, fj=None: (seen.append((sym, side)) or ("ORDER_FLOW:3_of_6_against_short(delta)", None)))
    assert _plan(db, cand(), None, {"conviction": 70}) is None
    assert seen == [("X", "SHORT")]                                                     # the SHORT checklist, not the long one
    rows = db.query(models.ScalpCandidateLog).all()
    assert len(rows) == 1 and rows[0].decision == "SHORT_SKIP" and rows[0].reason.startswith("SHORT_ORDER_FLOW:")
    assert db.query(models.ScalpShortShadow).count() == 0
    assert se.summary(db)["gate_counters"]["by_code"]["SHORT_ORDER_FLOW"] == 1


def test_e269_no_tape_opinion_leaves_the_shadow_short_untouched_and_plan_carries_the_tape(db, monkeypatch):
    monkeypatch.setattr(se.flow_gate, "entry_check", lambda sym, side, fj=None: (None, '{"tape":{"against":1}}'))
    plan = se.plan_short(db, cand(), None, {"conviction": 70}, NOW)
    assert plan is not None and plan["tape_json"] == '{"tape":{"against":1}}'
    assert _plan(db, cand("Y"), None, {"conviction": 70}) is not None


def test_a_symbol_held_as_a_long_is_refused(db, monkeypatch):
    class _Q:
        def __init__(self, hit): self.hit = hit
        def filter(self, *a): return self
        def first(self): return self.hit
        def all(self): return []
    real = db.query
    monkeypatch.setattr(db, "query", lambda m, *a: _Q(object()) if m is models.ScalpPosition else real(m, *a))
    assert _plan(db, cand()) is None
    assert real(models.ScalpShortShadow).count() == 0


def test_no_capital_is_a_refusal_not_a_guess(db):
    row = ledger._get_or_create(db)
    row.available_capital = 1.0
    db.commit()
    assert _plan(db, cand()) is None
    assert db.query(models.ScalpCandidateLog).one().reason.startswith("SHORT_NO_CAPITAL")


def _open_row(db, **kw):
    d = dict(day="2026-10-23", symbol="X", entry_price=100.0, stop_price=101.0, target_price=98.0, quantity=10, status="OPEN", low_water=100.0, high_water=100.0)
    d.update(kw)
    r = models.ScalpShortShadow(**d)
    db.add(r)
    db.commit()
    return r


def test_expire_open_marks_leftover_rows_expired_without_a_result(db):
    r1, r2 = _open_row(db, symbol="A"), _open_row(db, symbol="B", day="2026-10-22")
    done = _open_row(db, symbol="C", status="TARGET", exit_price=98.0, gross_pnl=20.0, net_pnl=18.0)
    assert se.expire_open(db, NOW) == 2
    for r in (r1, r2, done):
        db.refresh(r)
    assert r1.status == "EXPIRED" and r2.status == "EXPIRED" and r1.net_pnl is None and r1.exit_price is None and r1.closed_at is not None
    assert done.status == "TARGET" and done.net_pnl == 18.0                    # a closed row is never touched
    assert se.expire_open(db, NOW) == 0                                        # second pass: nothing left


def test_expire_open_never_follows_a_price(db, monkeypatch):
    r = _open_row(db)
    monkeypatch.setattr(se, "_live_price", lambda s: (_ for _ in ()).throw(AssertionError("followed a price")))
    assert se.expire_open(db, NOW) == 1
    db.refresh(r)
    assert r.status == "EXPIRED" and r.low_water == 100.0


def test_the_follower_is_gone():
    assert not hasattr(se, "resolve_open") and not hasattr(se, "_close")


def test_summary_has_no_shadow_history(db):
    _open_row(db, symbol="A", status="TARGET", exit_price=98.0, gross_pnl=20.0, net_pnl=18.0)
    s = se.summary(db)
    assert set(s) == {"mode", "gate_counters", "shortlist"}                      # E285 round 94: no rows, totals or S2 progress
    assert not hasattr(se, "_row_dict")


def test_cycle_in_off_mode_places_nothing_and_does_not_scan(db, monkeypatch):
    monkeypatch.setattr(config, "SHORT_MODE", "off")
    from screening import engine
    monkeypatch.setattr(engine, "scan_shorts", lambda *a, **k: (_ for _ in ()).throw(AssertionError("scanned in off mode")))
    out = asyncio.run(se.run_short_cycle(db, NOW))
    assert out["mode"] == "off" and out["recorded"] is None


def test_cycle_in_off_mode_still_expires_leftover_rows(db, monkeypatch):
    r = _open_row(db)
    monkeypatch.setattr(config, "SHORT_MODE", "off")
    assert asyncio.run(se.run_short_cycle(db, NOW))["resolved"] == 1
    db.refresh(r)
    assert r.status == "EXPIRED"


def test_live_cycle_hands_only_the_board_ranked_faller_to_short_live(db, monkeypatch):
    import screening.quality_gate as qg
    from screening import engine
    from orders import short_live
    import tz_utils
    monkeypatch.setattr(tz_utils, "is_market_open_ist", lambda *a, **k: True)
    monkeypatch.setattr(engine, "scan_shorts", lambda held=None: [cand("A"), cand("NOBOARD")])
    monkeypatch.setattr(se, "_bear_rows", lambda: {"A": {"side": "bear", "conviction": 70, "symbol": "A"}})
    tried = []
    monkeypatch.setattr(short_live, "automatic_entry_off", lambda db: None)             # live mode asks whether auto entries are on (module, auto-pilot, armed)
    monkeypatch.setattr(short_live, "attempt_short_live", lambda db, c, q, brow, now=None: (tried.append(c.symbol) or object()))

    async def _check(sym, cached=None):
        return types.SimpleNamespace(event_unchecked=False, has_positive_catalyst=False, bulk_deal_flag=False, earnings_date=None)
    monkeypatch.setattr(qg, "check", _check)
    monkeypatch.setattr(qg, "get_cache_batch", lambda *a, **k: {})
    out = asyncio.run(se.run_short_cycle(db, NOW))
    assert out["recorded"] == 1 and tried == ["A"]
