"""group 466 - position-stocks close-all helpers (confirm gate, pass merge, summary). Pure functions, no DB or broker."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from orders import close_all as ca
from orders import statuses


@pytest.mark.parametrize("v,ok", [("CLOSE ALL", True), (" close all ", True), ("", False), ("CLOSE", False), (None, False)])
def test_confirm(v, ok):
    assert ca.confirm_ok(v) is ok


def test_status_registered_as_flat_sell():
    assert ca.CLOSE_REASON in statuses.FLAT_SELL_STATUSES and ca.CLOSE_REASON in statuses.TERMINAL_STATUSES


def test_merge_second_pass_fixes_first_failure():
    first = {"found": 2, "sent": 1, "symbols": ["A"], "failed": [{"symbol": "B", "error": "x"}]}
    second = {"found": 1, "sent": 1, "symbols": ["B"], "failed": []}
    m = ca.merge(first, second)
    assert m["sent"] == 2 and m["failed"] == [] and m["found"] == 2


def test_merge_keeps_persistent_failure():
    first = {"symbols": [], "failed": [{"symbol": "B", "error": "x"}]}
    second = {"symbols": [], "failed": [{"symbol": "B", "error": "y"}]}
    m = ca.merge(first, second)
    assert m["failed"] == [{"symbol": "B", "error": "y"}] and m["sent"] == 0


def test_summary_names_failures():
    s = ca.summary({"sent": 1, "found": 2, "failed": [{"symbol": "B", "error": "x"}]})
    assert "1 of 2" in s and "FAILED: B" in s


def test_disarm_flips_flag_and_reports_prior_state():
    class G: is_armed = True
    class DB:
        committed = 0
        def commit(self): self.committed += 1
        def rollback(self): pass
    g, db = G(), DB()
    assert ca.disarm(db, lambda _db: g) is True and g.is_armed is False and db.committed == 1
    assert ca.disarm(db, lambda _db: g) is False


def test_disarm_never_raises():
    class DB:
        def rollback(self): pass
    def boom(_db): raise RuntimeError("x")
    assert ca.disarm(DB(), boom) is False
