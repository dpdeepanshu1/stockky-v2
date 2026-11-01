"""E274 round 42: the Market Board is a CONFIRMATION for S4 (negative-event short), not a hard refusal; the old shortlist path keeps it as a hard rule."""
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
from orders import setup_engine, setup_short, short_entry as se, short_gates

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 2, 10, 30, tzinfo=IST)
BEAR = {"side": "bear", "conviction": 70}


def facts(**kw):
    f = {"side": "SHORT", "service": "PS", "now": NOW, "allowed": {setup_engine.S4}, "event_polarity": -1, "price": 95.0, "preopen_price": 98.0, "or_low": 96.0,
         "bar_close": 95.5, "bar_closed_after_range": True, "tape": 0, "sector_third": "mid"}
    f.update(kw)
    return f


# ---- engine ---------------------------------------------------------------
def test_board_bearish_counts_as_a_confirmation_when_the_fact_is_present():
    r = setup_engine.classify(facts(board_bear=True, above_vwap=False))
    assert r["id"] == "S4_CATALYST_DOWN" and "board_bearish" in r["confirmations"] and "below_vwap" in r["confirmations"]


def test_board_alone_is_not_enough():
    assert setup_engine.classify(facts(board_bear=True))["id"] is None


def test_without_the_fact_s4_keeps_its_three_confirmations():
    assert setup_engine.classify(facts(above_vwap=False))["id"] is None                       # one of 3
    r = setup_engine.classify(facts(above_vwap=False, tape=-1))
    assert r["id"] == "S4_CATALYST_DOWN" and "board_bearish" not in r["confirmations"]


def test_a_board_that_is_not_bearish_adds_nothing():
    assert setup_engine.classify(facts(board_bear=False, above_vwap=False))["id"] is None


def test_the_long_side_never_sees_the_board_confirmation():
    f = facts(side="LONG", event_polarity=1, price=105.0, preopen_price=102.0, or_high=104.0, bar_close=104.5, board_bear=True, above_vwap=True, tape=1)
    f.pop("or_low"); f["allowed"] = {setup_engine.S1}
    r = setup_engine.classify(f)
    assert r["id"] == "S1_CATALYST_UP" and "board_bearish" not in r["confirmations"]


# ---- setup_short ----------------------------------------------------------
def test_board_bearish_needs_side_and_conviction(monkeypatch):
    assert setup_short.board_bearish(BEAR) is True
    assert setup_short.board_bearish({"side": "bear", "conviction": 49}) is False
    assert setup_short.board_bearish({"side": "bull", "conviction": 90}) is False
    assert setup_short.board_bearish(None) is False and setup_short.board_bearish({"side": "bear"}) is False
    monkeypatch.setattr(config, "SHORT_MIN_BOARD_CONVICTION", 80.0, raising=False)
    assert setup_short.board_bearish(BEAR) is False


def test_facts_short_carries_board_bear_only_with_the_switch(monkeypatch):
    kw = dict(symbol="X", price=95.0, now=NOW, event_polarity=-1, orb={"range_low": 96.0}, quote=None, gap_pct=-2.0, board_row=BEAR)
    assert setup_short.facts_short(**kw)["board_bear"] is True
    monkeypatch.setattr(config, "SETUP_S4_BOARD_CONFIRM_ENABLED", False, raising=False)
    assert "board_bear" not in setup_short.facts_short(**kw)


def test_decide_passes_the_board_row_to_the_engine(monkeypatch):
    seen = {}
    monkeypatch.setattr(setup_short.setup_engine, "classify", lambda f: (seen.update(f) or {"id": None}))
    q = types.SimpleNamespace(event_polarity=-1)
    setup_short.decide(symbol="X", price=95.0, now=NOW, quality=q, quote={}, orb={}, gap_pct=-1.0, inputs={}, board_row=BEAR)
    assert seen.get("board_bear") is True


# ---- gates ----------------------------------------------------------------
def test_skip_board_leaves_only_the_board_gate_out(monkeypatch):
    seen = []
    monkeypatch.setattr(short_gates, "GATES", tuple((n, (lambda f, now, n=n: seen.append(n))) for n, _ in short_gates.GATES))
    short_gates.evaluate({}, NOW, skip_board=True)
    assert "board" not in seen and "structure" in seen and "quality" in seen and "vwap" in seen
    seen.clear(); short_gates.evaluate({}, NOW)
    assert "board" in seen


def test_board_defers_only_on_a_negative_event(monkeypatch):
    neg, pos, none = types.SimpleNamespace(event_polarity=-1), types.SimpleNamespace(event_polarity=1), types.SimpleNamespace(event_polarity=None)
    assert se._board_defers(neg) and not se._board_defers(pos) and not se._board_defers(none) and not se._board_defers(None)
    monkeypatch.setattr(config, "SETUP_S4_BOARD_CONFIRM_ENABLED", False, raising=False)
    assert not se._board_defers(neg)


# ---- plan_short -----------------------------------------------------------
@pytest.fixture()
def env(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    row = ledger._get_or_create(s)
    row.total_allocated_capital, row.available_capital = 100000.0, 100000.0
    s.commit()
    se.reset()
    calls = {"evaluate": [], "board": 0, "decide": []}
    monkeypatch.setattr(config, "SHORT_MODE", "shadow", raising=False)
    monkeypatch.setattr(short_gates, "gather_facts", lambda *a, **k: {"live": 100.0, "quote": None})
    monkeypatch.setattr(short_gates, "evaluate", lambda f, now=None, **kw: (calls["evaluate"].append(kw) or None))
    box = {"board": None, "setup": {"id": None, "skip": "NO_SETUP"}}
    monkeypatch.setattr(short_gates, "board_gate_short", lambda f, now=None: (calls.__setitem__("board", calls["board"] + 1) or box["board"]))
    for mod, name in ((se.entry_pause, "all_paused"), (se.stopout_guard, "entry_reason"), (se.calendar_gate, "entry_block_reason"), (se.calendar_gate, "scalp_cutoff_reason"),
                      (se.depth_gate, "max_qty_from_book"), (se.depth_gate, "max_qty_from_depth20"), (se.cost_gate, "reject_reason"), (se.entry_pause, "symbol_blocked")):
        monkeypatch.setattr(mod, name, lambda *a, **k: None)
    monkeypatch.setattr(se.flow_gate, "entry_check", lambda *a, **k: (None, None))
    from screening import opening_gate
    monkeypatch.setattr(opening_gate, "stop_reject", lambda *a, **k: None)
    lv = types.SimpleNamespace(stop_price=101.0, target_price=98.0, stop_pct=1.0, target_pct=2.0, side="SHORT")
    monkeypatch.setattr(se, "compute_levels", lambda *a, **k: lv)
    monkeypatch.setattr(se.tm_gate, "check_ev", lambda *a, **k: (None, None))
    monkeypatch.setattr(se.setup_short, "decide", lambda **k: (calls["decide"].append(k) or box["setup"]))
    monkeypatch.setattr(se.squeeze_guard, "reason", lambda **k: None)
    yield types.SimpleNamespace(db=s, calls=calls, box=box)
    s.close()


def cand(sym="X"):
    return types.SimpleNamespace(symbol=sym, window_label="5m", pct_change=-1.5, composite_score=3.0, current_ltp=100.0)


NEG, PLAIN = types.SimpleNamespace(event_polarity=-1), types.SimpleNamespace(event_polarity=None)
S4 = {"id": "S4_CATALYST_DOWN", "size_mult": 0.5, "confirmations": ["tape_selling", "below_vwap"], "trigger": "x"}


def codes(db):
    return [r.reason.split(":")[0] for r in db.query(models.ScalpCandidateLog).all()]


def test_a_negative_event_short_skips_the_board_gate_and_goes_on_with_an_s4_match(env):
    env.box["setup"] = S4
    plan = se.plan_short(env.db, cand(), NEG, None, NOW)                       # no Board row at all
    assert plan is not None and plan["setup"]["id"] == "S4_CATALYST_DOWN"
    assert env.calls["evaluate"] == [{"skip_board": True}] and env.calls["board"] == 0
    assert env.calls["decide"][0]["board_row"] is None


def test_a_negative_event_short_with_no_s4_match_meets_the_board_rule(env):
    env.box["board"] = ("SHORT_NO_BOARD_ROW", "the Market Board has no row for this symbol")
    assert se.plan_short(env.db, cand(), NEG, None, NOW) is None
    assert env.calls["board"] == 1 and codes(env.db) == ["SHORT_NO_BOARD_ROW"]


def test_a_negative_event_short_with_no_s4_match_but_a_bearish_board_still_goes_on(env):
    assert se.plan_short(env.db, cand(), NEG, BEAR, NOW) is not None
    assert env.calls["board"] == 1


def test_without_a_negative_event_the_old_path_is_unchanged(env):
    assert se.plan_short(env.db, cand(), PLAIN, BEAR, NOW) is not None
    assert env.calls["evaluate"] == [{}] and env.calls["board"] == 0


def test_the_switch_off_restores_the_hard_board_rule(env, monkeypatch):
    monkeypatch.setattr(config, "SETUP_S4_BOARD_CONFIRM_ENABLED", False, raising=False)
    se.plan_short(env.db, cand(), NEG, None, NOW)
    assert env.calls["evaluate"] == [{}] and env.calls["board"] == 0


def test_a_board_gate_that_raises_refuses(env, monkeypatch):
    def boom(f, now=None):
        raise RuntimeError("x")
    monkeypatch.setattr(short_gates, "board_gate_short", boom)
    assert se.plan_short(env.db, cand(), NEG, None, NOW) is None and codes(env.db) == ["SHORT_GATE_ERROR"]


# ---- the cycle ------------------------------------------------------------
def test_the_cycle_checks_off_board_names_and_keeps_only_the_negative_event_ones(env, monkeypatch):
    import screening.engine as eng_mod
    from screening import quality_gate, scan_hits
    import tz_utils
    monkeypatch.setattr(tz_utils, "is_market_open_ist", lambda *a, **k: True)
    monkeypatch.setattr(short_gates, "time_window", lambda f, now, early=False: None)
    monkeypatch.setattr(short_gates, "early_open", lambda now: False)
    monkeypatch.setattr(se, "resolve_open", lambda db, now=None: 0)
    monkeypatch.setattr(se, "_bear_rows", lambda: {"BEARISH": dict(BEAR)})
    cs = [cand("BEARISH"), cand("OFFNEG"), cand("OFFPLAIN")]
    monkeypatch.setattr(eng_mod, "scan_shorts", lambda held: cs)
    monkeypatch.setattr(scan_hits, "record_scan", lambda *a, **k: None)
    monkeypatch.setattr(scan_hits, "mark_checked", lambda *a, **k: None)
    monkeypatch.setattr(quality_gate, "get_cache_batch", lambda db, syms: {})
    qmap = {"BEARISH": PLAIN, "OFFNEG": NEG, "OFFPLAIN": PLAIN}

    async def _check(sym, cached=None):
        return qmap[sym]
    monkeypatch.setattr(quality_gate, "check", _check)
    monkeypatch.setattr(se.tm_gate, "note_picks", lambda *a, **k: None)
    tried = []
    monkeypatch.setattr(se, "attempt_short_shadow", lambda db, c, q, brow, now=None: (tried.append((c.symbol, brow is not None)) or None))
    out = asyncio.run(se.run_short_cycle(env.db, NOW))
    assert out["recorded"] == 0 and sorted(tried) == [("BEARISH", True), ("OFFNEG", False)]       # OFFPLAIN (no news, no Board) is dropped
    monkeypatch.setattr(config, "SETUP_S4_BOARD_CONFIRM_ENABLED", False, raising=False)
    tried.clear(); asyncio.run(se.run_short_cycle(env.db, NOW))
    assert tried == [("BEARISH", True)]
