"""Batch 1 (2026-11-02): a stage that swallowed a failed flush must not poison the next stage."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import cycle_runner


class FakeDB:
    def __init__(self, active):
        self.is_active = active
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1
        self.is_active = True


def test_rolls_back_only_when_session_is_in_failed_flush_state():
    bad = FakeDB(active=False)
    assert cycle_runner._heal_session(bad, "entry") is True
    assert bad.rollbacks == 1 and bad.is_active
    ok = FakeDB(active=True)
    assert cycle_runner._heal_session(ok, "entry") is False
    assert ok.rollbacks == 0


def test_switch_off_leaves_session_alone(monkeypatch):
    monkeypatch.setenv("CYCLE_SESSION_HEAL", "false")
    bad = FakeDB(active=False)
    assert cycle_runner._heal_session(bad, "entry") is False
    assert bad.rollbacks == 0


def test_never_raises_when_rollback_itself_fails():
    class Boom(FakeDB):
        def rollback(self):
            raise RuntimeError("conn gone")
    assert cycle_runner._heal_session(Boom(active=False), "exit") is False
