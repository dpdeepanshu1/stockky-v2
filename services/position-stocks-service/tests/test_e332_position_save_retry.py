"""
E332 - position-stocks: the BUY/SELL is already live at the broker when the local position row is saved.

Before: attempt_entry and attempt_manual_entry had no handling at all - one failed commit raised out of the entry with a live order, no row, the
symbol claim and the capital still held, and the circuit breaker seeing a "crash". short_live alerted but never retried.
Now: orders/position_save.save_new_position() rolls back and writes the same row again (ENTRY_POSITION_SAVE_RETRIES tries, short backoff). If every
try fails: loud log + Telegram, capital and symbol claim stay held (the order is live), the symbol rests for the session, nothing raises out of the
automatic entry; the manual entry raises ManualEntryRejected with the order id. A failed order-count write no longer blocks the position row.

Run from services/position-stocks-service:  python -m pytest tests/test_e332_position_save_retry.py -q
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import models  # noqa: E402
from orders import entry, entry_pause, position_save  # noqa: E402
from tests.test_entry import (  # noqa: E402,F401  (env is a fixture)
    LEDGER_AVAILABLE, POSITION_VALUE, available, cand, env, gate_of, last_log, lock_held, n_positions,
)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(config, "ENTRY_POSITION_SAVE_RETRIES", 3, raising=False)
    monkeypatch.setattr(config, "ENTRY_POSITION_SAVE_BACKOFF_S", 0.0, raising=False)
    entry_pause.reset() if hasattr(entry_pause, "reset") else None


def _fail_commits(db, *, kind, times):
    """Make db.commit() raise `times` times while an object of `kind` is pending (new) or modified (dirty)."""
    real, st = db.commit, {"n": 0}

    def commit():
        pend = list(db.new) + list(db.dirty)
        if st["n"] < times and any(isinstance(o, kind) for o in pend):
            st["n"] += 1
            raise RuntimeError("oracle gone")
        return real()
    db.commit = commit
    return st


# ── the helper ──────────────────────────────────────────────────────────────
class _FakeDb:
    def __init__(self, commit_fails=0, refresh_fails=False, rollback_fails=False):
        self.commit_fails, self.refresh_fails, self.rollback_fails = commit_fails, refresh_fails, rollback_fails
        self.added, self.rollbacks, self.commits = [], 0, 0

    def add(self, o): self.added.append(o)

    def commit(self):
        self.commits += 1
        if self.commit_fails > 0:
            self.commit_fails -= 1
            raise RuntimeError("busy")

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_fails:
            raise RuntimeError("rollback broke")

    def refresh(self, o):
        if self.refresh_fails:
            raise RuntimeError("gone")


def test_helper_first_try_ok():
    db, sl = _FakeDb(), []
    assert position_save.save_new_position(db, object(), sleep=sl.append) is True
    assert db.commits == 1 and db.rollbacks == 0 and sl == []


def test_helper_retries_with_growing_backoff(monkeypatch):
    monkeypatch.setattr(config, "ENTRY_POSITION_SAVE_BACKOFF_S", 0.5)
    db, sl = _FakeDb(commit_fails=2), []
    assert position_save.save_new_position(db, object(), sleep=sl.append) is True
    assert db.commits == 3 and db.rollbacks == 2 and sl == [0.5, 1.0]


def test_helper_gives_up_after_the_configured_tries(monkeypatch):
    monkeypatch.setattr(config, "ENTRY_POSITION_SAVE_RETRIES", 2)
    db, sl = _FakeDb(commit_fails=99), []
    assert position_save.save_new_position(db, object(), sleep=sl.append) is False
    assert db.commits == 2 and len(sl) == 1                      # no sleep after the last try


def test_helper_survives_a_broken_rollback():
    db = _FakeDb(commit_fails=1, rollback_fails=True)
    assert position_save.save_new_position(db, object(), sleep=lambda s: None) is True


def test_helper_refresh_failure_after_a_good_commit_is_still_saved():
    db = _FakeDb(refresh_fails=True)
    assert position_save.save_new_position(db, object(), sleep=lambda s: None) is True
    assert db.commits == 1


@pytest.mark.parametrize("bad", [float("nan"), -1.0, 99.0, "x"])
def test_helper_junk_backoff_falls_back(monkeypatch, bad):
    monkeypatch.setattr(config, "ENTRY_POSITION_SAVE_BACKOFF_S", bad)
    assert position_save._backoff() == 0.3


@pytest.mark.parametrize("bad,want", [(0, 1), (-4, 1), ("x", 3)])
def test_helper_junk_retry_count(monkeypatch, bad, want):
    monkeypatch.setattr(config, "ENTRY_POSITION_SAVE_RETRIES", bad)
    assert position_save._tries() == want


def test_alert_never_raises(monkeypatch):
    import notifier
    monkeypatch.setattr(notifier, "notify_fire_and_forget", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tg down")))
    position_save.alert_unrecorded("BUY", "ABC", 3, "SO1", "x")


# ── automatic BUY ───────────────────────────────────────────────────────────
class TestAutoBuy:
    def test_one_failed_save_is_retried_and_the_position_is_recorded(self, env):
        db, b, sent = env
        st = _fail_commits(db, kind=models.ScalpPosition, times=1)
        pos = entry.attempt_entry(db, cand("ABC"))
        assert pos is not None and st["n"] == 1
        assert n_positions(db) == 1 and len(b.of("place_super_order")) == 1     # ONE order, ONE row
        assert lock_held(db, "ABC")
        assert not any("NOT recorded" in m for m in sent["info"])

    def test_every_save_fails_keeps_capital_and_claim_and_alerts(self, env):
        db, b, sent = env
        _fail_commits(db, kind=models.ScalpPosition, times=99)
        assert entry.attempt_entry(db, cand("ABC")) is None                      # does not raise out of the entry
        assert n_positions(db) == 0
        assert len(b.of("place_super_order")) == 1                               # never a second BUY
        assert available(db) == pytest.approx(LEDGER_AVAILABLE - POSITION_VALUE)  # the live order still holds its capital
        assert lock_held(db, "ABC")
        assert any("BUY placed but NOT recorded" in m for m in sent["info"])
        assert last_log(db).reason.startswith("POSITION_NOT_RECORDED:")
        assert entry_pause.symbol_blocked("ABC")                                  # rested for the session

    def test_failed_order_count_write_does_not_block_the_row(self, env):
        db, b, _ = env
        st = _fail_commits(db, kind=models.ScalpGateState, times=1)
        pos = entry.attempt_entry(db, cand("ABC"))
        assert pos is not None and st["n"] == 1 and n_positions(db) == 1

    def test_log_failure_while_the_db_is_down_does_not_raise(self, env, monkeypatch):
        db, b, sent = env
        _fail_commits(db, kind=models.ScalpPosition, times=99)
        monkeypatch.setattr(entry, "_log_candidate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
        assert entry.attempt_entry(db, cand("ABC")) is None
        assert any("NOT recorded" in m for m in sent["info"])


# ── manual BUY ──────────────────────────────────────────────────────────────
class TestManualBuy:
    def test_one_failed_save_is_retried(self, env):
        db, b, _ = env
        st = _fail_commits(db, kind=models.ScalpPosition, times=1)
        pos = entry.attempt_manual_entry(db, "ABC", 500.0)
        assert pos is not None and st["n"] == 1 and n_positions(db) == 1
        assert len(b.of("place_super_order")) == 1

    def test_every_save_fails_raises_with_the_order_id_and_keeps_capital_and_claim(self, env):
        db, b, sent = env
        _fail_commits(db, kind=models.ScalpPosition, times=99)
        with pytest.raises(entry.ManualEntryRejected, match=r"SO1.*could not be saved"):
            entry.attempt_manual_entry(db, "ABC", 500.0)
        assert n_positions(db) == 0 and len(b.of("place_super_order")) == 1
        assert available(db) < LEDGER_AVAILABLE and lock_held(db, "ABC")
        assert any("Manual BUY placed but NOT recorded" in m for m in sent["info"])
        assert entry_pause.symbol_blocked("ABC")

    def test_failed_order_count_write_does_not_block_the_row(self, env):
        db, _, _ = env
        st = _fail_commits(db, kind=models.ScalpGateState, times=1)
        assert entry.attempt_manual_entry(db, "ABC", 500.0) is not None and st["n"] == 1
