"""E264 - no REAL BUY decision for a symbol Dhan cannot trade (listed only in series BE, or not listed).

30 Oct 2026: STLTECH, TBZ, MOREPENLAB (swing-lane ENTERs) and HFCL (main entry engine) were approved and then refused by Dhan with
"No Dhan NSE_EQ security_id found". `dhan_client.untradable_reason` now answers that at decision time; the entry engine WAITs and the
swing lane skips to its next pick.
Run from services/real-trade-service:  python3 -m pytest tests/test_e264_tradable_gate.py -q"""
from __future__ import annotations

import os
import re
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SVC = os.path.dirname(HERE)
sys.path.insert(0, SVC)

from execution import dhan_client as dc  # noqa: E402


@pytest.fixture(autouse=True)
def _loaded_list(monkeypatch):
    """A loaded security list: TCS and HFCL-style EQ symbols resolve, BEDOWN is listed only in series BE, nothing re-loads."""
    monkeypatch.setattr(dc, "_security_cache", {"TCS": "11536", "NTPC": "11630"})
    monkeypatch.setattr(dc, "_security_other_series", {"BEDOWN": "BE"})
    monkeypatch.setattr(dc, "_security_cache_loaded_at", time.time())
    monkeypatch.setattr(dc, "_security_load_failed_at", 0.0)
    monkeypatch.setattr(dc, "_security_miss_reload_at", time.time())          # a miss does not trigger a re-load in these tests
    monkeypatch.setattr(dc, "_load_security_cache", lambda db: None)


def test_eq_symbol_is_tradable():
    assert dc.untradable_reason(None, "TCS") is None
    assert dc.untradable_reason(None, "ntpc.ns") is None                       # same normalisation as the order path


def test_be_series_symbol_is_refused_with_the_series_in_the_reason():
    r = dc.untradable_reason(None, "BEDOWN")
    assert r and r.startswith("NOT_TRADABLE_SERIES:")
    assert "'BEDOWN'" in r and "'BE'" in r
    assert "No Dhan NSE_EQ security_id found for" not in r                     # the prefix is replaced, not repeated


def test_symbol_missing_from_a_loaded_list_is_refused():
    r = dc.untradable_reason(None, "NOSUCHCO")
    assert r and r.startswith("NOT_TRADABLE_SERIES:") and "not in Dhan's NSE equity list" in r


def test_fails_open_when_the_list_did_not_load(monkeypatch):
    """An empty list means token / download trouble, not "this stock is not listed": the check must not block on that."""
    monkeypatch.setattr(dc, "_security_cache", {})
    monkeypatch.setattr(dc, "_security_cache_loaded_at", 0.0)
    assert dc.untradable_reason(None, "TCS") is None
    assert dc.untradable_reason(None, "BEDOWN") is None


def test_fails_open_on_an_unexpected_error(monkeypatch):
    def boom(db, symbol):
        raise RuntimeError("anything else")
    monkeypatch.setattr(dc, "get_security_id", boom)
    assert dc.untradable_reason(None, "TCS") is None


def test_the_order_path_still_raises_for_the_same_symbol():
    """The check is advisory; get_security_id (the order path) is unchanged and still refuses."""
    with pytest.raises(dc.SecurityNotResolvedError):
        dc.get_security_id(None, "BEDOWN")


# ── wiring (source order): the gate sits before anything that creates an order ──────────────────────────────────────────

def _src(*parts):
    with open(os.path.join(SVC, *parts), encoding="utf-8") as f:
        return f.read()


def test_entry_engine_checks_tradability_before_the_circuit_gate_and_only_for_real():
    s = _src("entry_engine", "entry.py")
    a = s.index("dhan_client.untradable_reason(db, cand.symbol)")
    assert a < s.index("circuit_gate.reject_reason_tick(tick)")               # decided before the later gates and long before the order row
    assert a < s.index("db.add(models.TradeOrder(") if "db.add(models.TradeOrder(" in s else True
    block = s[s.rindex("# ── Gate 4a (E264)", 0, a):a]
    assert 'mode == "REAL"' in block and "ENTRY_TRADABLE_GATE_ENABLED" in block
    after = s[a:a + 900]
    assert 'decision.gate_tag = "not_tradable"' in after and "continue" in after   # a WAIT, then on to the next candidate


def test_swing_lane_skips_an_untradable_pick_before_it_asks_for_an_order():
    s = _src("swing", "live.py")
    a = s.index("dhan_client.untradable_reason(db, sym)")
    assert a < s.index("manual_engine.evaluate_manual_order(")
    tail = s[a:a + 200]
    assert "skip(_nt)" in tail and "continue" in tail                          # recorded as SKIPPED, loop moves to the next pick
    assert 'mode == "REAL"' in s[s.rindex("if mode", 0, a):a + 40]


def test_flag_is_on_by_default_and_in_the_control_catalog():
    cfg = _src("config.py")
    assert re.search(r'ENTRY_TRADABLE_GATE_ENABLED = \(\(os\.getenv\("ENTRY_TRADABLE_GATE_ENABLED"\) or ""\)\.strip\(\) or "true"\)', cfg)
    cat = _src("control", "catalog.py")
    assert 'Flag("ENTRY_TRADABLE_GATE_ENABLED"' in cat and 'targets=(("config", "ENTRY_TRADABLE_GATE_ENABLED"),)' in cat
