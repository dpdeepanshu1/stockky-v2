"""
E336 - position-stocks entry: a failing audit-row write AFTER the BUY was sent must not change what happens to the claim, the position or the alerts.

Once the order step is reached, E334 deliberately leaves capital and claim alone, so anything that raised there stayed raised:
  * a BUY that definitively failed wrote its SKIPPED row BEFORE the symbol claim was released, so a DB error left the claim held for good;
  * attempt_entry's ENTERED row, written after the position was saved, skipped the Telegram alert, the approve-first mark and the fill correction;
  * the manual path's ENTERED commit turned a live, saved position into an error for the caller; its failed-BUY row skipped the unknown-BUY cooldown and alert.
The rows are audit lines, not gates: each write is now best effort (warning logged), and the work around it always runs.

Run from services/position-stocks-service:  python -m pytest tests/test_e336_log_write_after_buy.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orders import entry  # noqa: E402
from tests.test_entry import (  # noqa: E402,F401  (env is a fixture)
    LEDGER_AVAILABLE, available, cand, env, lock_held, n_positions,
)


def _fail_log_for(monkeypatch, decision):
    real = entry._log_candidate

    def _log(db, candidate, dec, reason, *a, **k):
        if dec == decision:
            raise RuntimeError("db commit failed")
        return real(db, candidate, dec, reason, *a, **k)
    monkeypatch.setattr(entry, "_log_candidate", _log)


def _fail_manual_row_for(monkeypatch, decision):
    real = entry.ScalpCandidateLog

    class _Row(real):
        def __init__(self, *a, **k):
            if k.get("decision") == decision:
                raise RuntimeError("db commit failed")
            super().__init__(*a, **k)
    monkeypatch.setattr(entry, "ScalpCandidateLog", _Row)


class TestAutoEntry:
    def test_failed_buy_with_a_failing_skip_row_still_releases_capital_and_claim(self, env, monkeypatch):
        db, b, _ = env
        b.super_script = [RuntimeError("gateway timeout talking to broker")]
        _fail_log_for(monkeypatch, "SKIPPED")
        assert entry.attempt_entry(db, cand("ABC")) is None            # not an exception
        assert not lock_held(db, "ABC")
        assert available(db) == pytest.approx(LEDGER_AVAILABLE)

    def test_a_failing_entered_row_still_alerts_marks_approval_and_returns_the_position(self, env, monkeypatch):
        db, b, sent = env
        marks = []
        monkeypatch.setattr(entry.approve_first, "mark_entered", lambda sym, details=None, now=None: marks.append(sym))
        _fail_log_for(monkeypatch, "ENTERED")
        pos = entry.attempt_entry(db, cand("ABC"))
        assert pos is not None and n_positions(db) == 1
        assert marks == ["ABC"]
        assert any("BUY placed" in m for m in sent["info"])

    def test_normal_failed_buy_still_logs_the_skip_reason(self, env):
        db, b, _ = env
        b.super_script = [RuntimeError("gateway timeout talking to broker")]
        assert entry.attempt_entry(db, cand("ABC")) is None
        assert (entry.last_skip_reason() or "").startswith("ORDER_FAILED")


class TestManualEntry:
    def test_a_failing_entered_row_still_returns_the_saved_position(self, env, monkeypatch):
        db, b, sent = env
        _fail_manual_row_for(monkeypatch, "ENTERED")
        pos = entry.attempt_manual_entry(db, "ABC", 500.0)
        assert pos is not None and n_positions(db) == 1
        assert any("Manual BUY placed" in m for m in sent["info"])

    def test_a_failing_skip_row_still_gives_the_normal_rejection_and_releases_the_claim(self, env, monkeypatch):
        db, b, _ = env
        b.super_script = [RuntimeError("gateway timeout talking to broker")]
        _fail_manual_row_for(monkeypatch, "SKIPPED")
        with pytest.raises(entry.ManualEntryRejected):
            entry.attempt_manual_entry(db, "ABC", 500.0)
        assert not lock_held(db, "ABC") and available(db) == pytest.approx(LEDGER_AVAILABLE)
