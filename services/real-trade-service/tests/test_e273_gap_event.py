"""E273 round 20: Real Trade event-aware gap-up rule. Plain runner: python3 tests/test_e273_gap_event.py (service folder)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))

import config  # noqa: E402
from entry_engine import gap_event as ge  # noqa: E402
from entry_engine import opening_gate as og  # noqa: E402
from test_e273_opening_gate_penalty import Env, NOW  # noqa: E402

EV = {"polarity": 1, "type": "order_win", "strength": 0.8}


def test_explains_gap_needs_positive_and_strength():
    assert ge.explains_gap(EV) is True
    assert ge.explains_gap({"polarity": 1, "strength": 0.1}) is False       # under GAP_EVENT_MIN_STRENGTH 0.3
    assert ge.explains_gap({"polarity": -1, "strength": 0.9}) is False
    assert ge.explains_gap({"polarity": 0, "strength": 0.9}) is False
    assert ge.explains_gap(None) is False and ge.explains_gap("x") is False
    old = config.GAP_EVENT_RULE_ENABLED
    config.GAP_EVENT_RULE_ENABLED = False
    try:
        assert ge.explains_gap(EV) is False and ge.enabled() is False
    finally:
        config.GAP_EVENT_RULE_ENABLED = old


def test_gap_kind_reads_real_reasons():
    assert ge.gap_kind("OPENING_GATE:EXTENDED:+4.00% > +3.00%") == "up"
    assert ge.gap_kind("PREOPEN_GAP_UP: indicative open +7.0% vs previous close") == "up"
    assert ge.gap_kind("OPENING_GATE:BELOW_PREV_CLOSE:-1.00% < -0.50%") == "down"
    assert ge.gap_kind("PREOPEN_GAP_DOWN: x") == "down" and ge.gap_kind("OPENING_GATE:NEAR_DAY_HIGH") is None and ge.gap_kind(None) is None


def test_large_gap_penalty_and_unconfirmed_reason():
    assert ge.large_gap_penalty(6.0) == 1.0 and ge.large_gap_penalty(7.5) == 1.0 and ge.large_gap_penalty(4.0) is None and ge.large_gap_penalty(None) is None
    assert ge.unconfirmed_reason([], None) is None
    assert ge.unconfirmed_reason(["OPENING_GATE:EXTENDED:+4%"], ge.S1_ID) is None
    r = ge.unconfirmed_reason(["OPENING_GATE:EXTENDED:+4%"], None)
    assert r.startswith("GAP_UP_NOT_CONFIRMED") and "OPENING_GATE:EXTENDED" in r


def test_opening_gate_waive_skips_only_the_extended_check():
    e = Env()
    try:
        t = SimpleNamespace(price=104.0, prev_close=100.0, day_high=104.2, day_low=102.0, atr=2.0, symbol="ABC")        # +4 % vs previous close
        assert og.reject_reason("REAL", t, NOW).startswith("OPENING_GATE:EXTENDED")
        r = og.reject_reason("REAL", t, NOW, waive_gap_up=True)
        assert r is None or not r.startswith("OPENING_GATE:EXTENDED")
        t2 = SimpleNamespace(price=104.0, prev_close=100.0, day_high=104.0, day_low=102.0, atr=2.0, symbol="ABC")      # the very high: still refused
        assert og.reject_reason("REAL", t2, NOW, waive_gap_up=True).startswith("OPENING_GATE:NEAR_DAY_HIGH")
        t3 = SimpleNamespace(price=97.0, prev_close=100.0, day_high=101.0, day_low=96.0, atr=2.0, symbol="ABC")        # a gap down past the hard ceiling is never waived (round 27: -1 % is only sized down)
        assert og.reject_reason("REAL", t3, NOW, waive_gap_up=True).startswith("OPENING_GATE:BELOW_PREV_CLOSE")
    finally:
        e.done()


def test_against_reason_names_the_event_and_the_gap():
    r = ge.against_reason("OPENING_GATE:BELOW_PREV_CLOSE:-1.00% < -0.50%", EV)
    assert r.startswith("GAP_AGAINST_EVENT:positive order_win (strength 0.80)") and "BELOW_PREV_CLOSE" in r
    assert ge.against_reason("PREOPEN_GAP_DOWN: x", None).startswith("GAP_AGAINST_EVENT:positive event (strength 0.00)")


def test_gap_down_wiring_consumes_the_candidate_with_its_own_reason():
    src = (HERE / "entry_engine" / "entry.py").read_text()
    assert "gap_event.against_reason(_og_reason, _gap_ev)" in src and "gate_tag = \"gap_against_event\"" in src
    assert "return gap_event.against_reason(reason, ev)" in src
    assert src.index("if _gap_against:") < src.index("# ── Gate 1: actionable decision label")


def test_wiring_and_switch():
    src = (HERE / "entry_engine" / "entry.py").read_text()
    assert "waive_gap_up=True" in src and "_preopen_gap_or_waive(cand.symbol, db, _gap_waived, _soft_pen)" in src and "gap_event.unconfirmed_reason(_gap_waived" in src
    assert src.index("gap_event.unconfirmed_reason(_gap_waived") < src.index("max_trade_risk = account_state.equity")
    assert "gap_event.LARGE_KEY" in src
    assert config.GAP_EVENT_RULE_ENABLED is True and config.GAP_EVENT_MIN_STRENGTH == 0.3
    assert 'Flag("GAP_EVENT_RULE_ENABLED"' in (HERE / "control" / "catalog.py").read_text()


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("ok", name)
    print(n, "passed")
