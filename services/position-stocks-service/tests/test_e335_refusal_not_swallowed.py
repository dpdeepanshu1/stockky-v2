"""
E335 - position-stocks entry: a refusal made inside a swallow-everything try must not be lost when its log write fails.

Three soft-penalty blocks in _attempt_entry_impl (below-open, opening-gate parts, pre-open gap) wrapped their refusal branch - release the symbol claim, write the
SKIPPED row, return - inside `try: ... except Exception: logger.debug("entry unaffected")`. _log_candidate re-raises a failed commit, so a DB hiccup there was
swallowed AFTER the claim had been released: the entry then went on, past a gate that had refused it, with no symbol claim held. The refusal is now decided inside the
try and acted on outside it, so a failing log write propagates (E334 then frees whatever is still held) instead of letting the entry through.

Run from services/position-stocks-service:  python -m pytest tests/test_e335_refusal_not_swallowed.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orders import entry  # noqa: E402
from screening import opening_gate  # noqa: E402
from tests.test_entry import (  # noqa: E402,F401  (env is a fixture)
    LEDGER_AVAILABLE, available, cand, env, lock_held,
)


def _fail_skip_log(monkeypatch):
    real = entry._log_candidate

    def _log(db, candidate, decision, reason, *a, **k):
        if decision == "SKIPPED":
            raise RuntimeError("db commit failed")
        return real(db, candidate, decision, reason, *a, **k)
    monkeypatch.setattr(entry, "_log_candidate", _log)


def _below_open_too_far(monkeypatch):
    monkeypatch.setattr(opening_gate, "below_open_penalty", lambda sym, ltp, now=None: {"penalty": 99.0, "pct": 5.0})


def _gap_down_with_event(monkeypatch):
    monkeypatch.setattr(entry.gap_event, "positive_event", lambda q: {"kind": "event"})
    monkeypatch.setattr(entry.gap_event, "against_reason", lambda why, q: "GAP_AGAINST_EVENT")
    monkeypatch.setattr(opening_gate, "parts_penalties", lambda sym, ltp, now=None: {"gap_down": {"penalty": 1.0, "why": "GAP_DOWN"}})


class TestFailingLogWriteDoesNotLetTheEntryThrough:
    def test_below_open_too_far(self, env, monkeypatch):
        db, b, _ = env
        _below_open_too_far(monkeypatch)
        _fail_skip_log(monkeypatch)
        with pytest.raises(RuntimeError, match="db commit failed"):
            entry.attempt_entry(db, cand("ABC"))
        assert b.of("place_super_order") == [] and b.of("place_order") == []
        assert not lock_held(db, "ABC") and available(db) == pytest.approx(LEDGER_AVAILABLE)

    def test_gap_down_against_a_positive_event(self, env, monkeypatch):
        db, b, _ = env
        _gap_down_with_event(monkeypatch)
        _fail_skip_log(monkeypatch)
        with pytest.raises(RuntimeError, match="db commit failed"):
            entry.attempt_entry(db, cand("ABC"))
        assert b.of("place_super_order") == [] and b.of("place_order") == []
        assert not lock_held(db, "ABC") and available(db) == pytest.approx(LEDGER_AVAILABLE)


class TestRefusalsStillWork:
    def test_below_open_too_far_skips_and_releases(self, env, monkeypatch):
        db, b, _ = env
        _below_open_too_far(monkeypatch)
        assert entry.attempt_entry(db, cand("ABC")) is None
        assert (entry.last_skip_reason() or "").startswith("OPENING_GATE:BELOW_OPEN_TOO_FAR")
        assert b.of("place_super_order") == [] and not lock_held(db, "ABC")
        assert available(db) == pytest.approx(LEDGER_AVAILABLE)

    def test_gap_down_against_event_skips_and_releases(self, env, monkeypatch):
        db, b, _ = env
        _gap_down_with_event(monkeypatch)
        assert entry.attempt_entry(db, cand("ABC")) is None
        assert entry.last_skip_reason() == "GAP_AGAINST_EVENT"
        assert b.of("place_super_order") == [] and not lock_held(db, "ABC")

    def test_a_crash_in_the_penalty_calculation_itself_is_still_fail_open(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(opening_gate, "below_open_penalty", lambda *a, **k: (_ for _ in ()).throw(ValueError("data")))
        monkeypatch.setattr(opening_gate, "parts_penalties", lambda *a, **k: (_ for _ in ()).throw(ValueError("data")))
        assert entry.attempt_entry(db, cand("ABC")) is not None      # gate data failing never blocks the entry (unchanged)
