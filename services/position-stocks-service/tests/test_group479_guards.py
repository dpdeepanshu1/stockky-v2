"""group479: position-stocks parity with real-trade groups 470-472 - whole-day stop-out guard, minimum shares, 14:00 cut-off default, ORB recorder."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
from orders import orb_shadow, stopout_guard
from tz_utils import IST

ROOT = Path(__file__).resolve().parent.parent
MON = datetime(2026, 10, 19, tzinfo=IST)           # a Monday


def at(h, m, s=0):
    return MON.replace(hour=h, minute=m, second=s)


# ---- stop-out guard (pure parts) -----------------------------------------------------------------------------------
def test_tally_counts_only_losing_stop_statuses_since_cutoff():
    since = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)
    rows = [
        ("STOP_HIT", since + timedelta(hours=4), -12.0, "AAA"),
        ("STOP_BREACH_EXIT", since + timedelta(hours=5), -3.0, "bbb"),
        ("STOP_HIT", since + timedelta(hours=5), 4.0, "CCC"),                # a ratcheted stop that made money
        ("TARGET_HIT", since + timedelta(hours=6), -1.0, "DDD"),              # not a stop-out
        ("STOP_HIT", since - timedelta(hours=1), -9.0, "EEE"),                # yesterday
        ("STOP_HIT", None, -9.0, "FFF"),                                      # no close time
    ]
    n, syms = stopout_guard.tally(rows, since)
    assert n == 2 and syms == {"AAA", "BBB"}


def test_tally_never_raises_on_garbage():
    assert stopout_guard.tally([("STOP_HIT",)], None) == (0, set())
    assert stopout_guard.tally(None, None) == (0, set())


def test_pause_reason_follows_limit(monkeypatch):
    monkeypatch.setattr(config, "STOPOUT_DAY_PAUSE_N", 3, raising=False)
    assert stopout_guard.pause_reason_from(2) is None
    assert "PAUSED" in stopout_guard.pause_reason_from(3)
    monkeypatch.setattr(config, "STOPOUT_DAY_PAUSE_N", 0, raising=False)
    assert stopout_guard.pause_reason_from(50) is None


def test_symbol_reason_and_switch(monkeypatch):
    monkeypatch.setattr(config, "STOPOUT_SYMBOL_BLOCK", True, raising=False)
    assert "SYMBOL" in stopout_guard.symbol_reason("abc", {"ABC"})
    assert stopout_guard.symbol_reason("XYZ", {"ABC"}) is None
    monkeypatch.setattr(config, "STOPOUT_SYMBOL_BLOCK", False, raising=False)
    assert stopout_guard.symbol_reason("ABC", {"ABC"}) is None


def test_entry_reason_pause_then_symbol_and_off_switch(monkeypatch):
    monkeypatch.setattr(config, "STOPOUT_GUARD_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "STOPOUT_DAY_PAUSE_N", 3, raising=False)
    monkeypatch.setattr(config, "STOPOUT_SYMBOL_BLOCK", True, raising=False)
    monkeypatch.setattr(stopout_guard, "stopouts_today", lambda db, now=None: (3, {"AAA"}))
    assert "PAUSED" in stopout_guard.entry_reason(None, "ZZZ")
    monkeypatch.setattr(stopout_guard, "stopouts_today", lambda db, now=None: (1, {"AAA"}))
    assert "SYMBOL" in stopout_guard.entry_reason(None, "AAA")
    assert stopout_guard.entry_reason(None, "ZZZ") is None
    monkeypatch.setattr(config, "STOPOUT_GUARD_ENABLED", False, raising=False)
    assert stopout_guard.entry_reason(None, "AAA") is None


def test_entry_reason_fails_open_on_db_error(monkeypatch):
    monkeypatch.setattr(config, "STOPOUT_GUARD_ENABLED", True, raising=False)
    assert stopout_guard.entry_reason(object(), "AAA") is None          # a db object with no .query -> (0, set()) -> no pause


# ---- ORB recorder ---------------------------------------------------------------------------------------------------
@pytest.fixture
def orb_on(monkeypatch):
    monkeypatch.setattr(config, "ORB_MODE", "shadow", raising=False)
    orb_shadow._reset_for_tests()
    yield
    orb_shadow._reset_for_tests()


def q(**kw):
    base = {"price": 101.0, "day_high": 100.0, "day_low": 98.0, "avg_price": 99.5, "spread_pct": 0.1}
    base.update(kw)
    return base


def test_orb_would_enter_after_range_and_break(orb_on):
    orb_shadow.record_quote("ABC", q(), at(9, 22))                       # range 98..100 fixed
    v = orb_shadow.evaluate("ABC", q(day_high=101.5), 101.0, at(9, 40))
    assert v["would_enter"] is True and v["range_high"] == 100.0 and v["range_low"] == 98.0
    assert v["stop_price"] == 98.0 and v["limit_price"] == 100.1


def test_orb_reasons(orb_on):
    orb_shadow.record_quote("ABC", q(), at(9, 22))
    assert "below_range_high" in orb_shadow.evaluate("ABC", q(), 99.0, at(9, 40))["reasons"]
    assert "below_vwap" in orb_shadow.evaluate("ABC", q(avg_price=102.0), 101.0, at(9, 40))["reasons"]
    assert "wide_spread" in orb_shadow.evaluate("ABC", q(spread_pct=0.9), 101.0, at(9, 40))["reasons"]


def test_orb_no_range_when_first_seen_late(orb_on):
    v = orb_shadow.evaluate("NEW", q(), 101.0, at(9, 50))
    assert v["would_enter"] is False and v["reason"] == "no_range"


def test_orb_silent_after_latest_entry_and_when_off(orb_on, monkeypatch):
    orb_shadow.record_quote("ABC", q(), at(9, 22))
    assert orb_shadow.evaluate("ABC", q(), 101.0, at(11, 0)) is None
    monkeypatch.setattr(config, "ORB_MODE", "off", raising=False)
    assert orb_shadow.evaluate("ABC", q(), 101.0, at(9, 40)) is None


def test_orb_attach_merges_into_flow_json(orb_on):
    orb_shadow.record_quote("ABC", q(), at(9, 22))
    out = orb_shadow.attach('{"v":1,"spread_pct":0.1}', "ABC", q(day_high=101.5), 101.0, at(9, 40))
    d = json.loads(out)
    assert d["spread_pct"] == 0.1 and d["orb"]["would_enter"] is True
    assert orb_shadow.attach(None, "ABC", q(), 101.0, at(11, 0)) is None  # nothing recorded -> input returned
    assert json.loads(orb_shadow.attach(None, "ABC", q(), 101.0, at(9, 40)))["orb"]["v"] == 1


def test_orb_never_raises():
    assert orb_shadow.evaluate(None, None, None) is None or isinstance(orb_shadow.evaluate(None, None, None), dict)
    orb_shadow.record_quote(None, "junk")


# ---- shipped defaults (fresh interpreter, conftest patches cannot hide a drift) ---------------------------------------
def test_shipped_defaults_in_clean_interpreter():
    code = ("import sys,json; sys.path.insert(0,'.'); import config; "
            "print('R'+json.dumps([config.SCALP_ENTRY_CUTOFF_IST, config.ENTRY_MIN_QTY, config.STOPOUT_GUARD_ENABLED, config.STOPOUT_DAY_PAUSE_N, "
            "config.STOPOUT_SYMBOL_BLOCK, config.ORB_MODE]))")
    import os
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SCALP_", "ENTRY_", "STOPOUT_", "ORB_"))}
    r = subprocess.run([sys.executable, "-I", "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60)
    line = next(x for x in r.stdout.splitlines() if x.startswith("R"))
    assert json.loads(line[1:]) == ["14:00", 3, True, 3, True, "shadow"]


def test_cutoff_blocks_after_1400_by_default_value(monkeypatch):
    from orders import calendar_gate
    monkeypatch.setattr(config, "SCALP_ENTRY_CUTOFF_IST", "14:00", raising=False)
    assert calendar_gate.scalp_cutoff_reason(at(13, 59)) is None
    assert "SCALP_ENTRY_CUTOFF" in calendar_gate.scalp_cutoff_reason(at(14, 0, 1))


# ---- wiring (static: the DB-backed entry path needs sqlalchemy) -----------------------------------------------------------
def test_entry_py_wires_the_three_guards_in_order():
    src = (ROOT / "orders" / "entry.py").read_text()
    assert "stopout_guard.entry_reason(db, candidate.symbol)" in src
    assert "orb_shadow.attach(" in src
    assert 'f"MIN_QTY:qty={quantity}<{_min_q}' in src
    assert src.index("stopout_guard.entry_reason") < src.index("# Max concurrent positions")
    assert src.index("MIN_QTY:qty=") < src.index("cost_gate.reject_reason(candidate.current_ltp, quantity")


def test_md_poller_feeds_the_orb_range():
    assert "orb_shadow.record_quote(sym, row)" in (ROOT / "feed" / "md_poller.py").read_text()
