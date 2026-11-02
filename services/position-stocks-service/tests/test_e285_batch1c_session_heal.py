"""E285 Batch 1 (position-stocks): a stage that swallowed a failed flush must not poison the next stage of the cycle."""
import os
import re

import main


class FakeDB:
    def __init__(self, active):
        self.is_active = active
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1
        self.is_active = True


def test_rolls_back_only_when_session_is_in_failed_flush_state():
    bad = FakeDB(active=False)
    assert main._heal_session(bad, "scan") is True and bad.rollbacks == 1 and bad.is_active
    ok = FakeDB(active=True)
    assert main._heal_session(ok, "scan") is False and ok.rollbacks == 0


def test_switch_off_leaves_session_alone(monkeypatch):
    monkeypatch.setenv("CYCLE_SESSION_HEAL", "false")
    bad = FakeDB(active=False)
    assert main._heal_session(bad, "scan") is False and bad.rollbacks == 0


def test_never_raises_when_rollback_itself_fails():
    class Boom(FakeDB):
        def rollback(self):
            raise RuntimeError("conn gone")
    assert main._heal_session(Boom(active=False), "scan") is False


def test_cycle_heals_before_each_later_stage_and_before_the_short_cycle():
    src = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "main.py"), encoding="utf-8").read()
    for stage in ("gate read", "EOD squareoff", "capital sync", "scan", "quality gate", "entry attempt", "short cycle"):
        assert f'_heal_session(db, "{stage}")' in src, stage
    order = [src.index(f'_heal_session(db, "{s}")') for s in ("gate read", "EOD squareoff", "capital sync", "scan", "quality gate", "entry attempt")]
    assert order == sorted(order)
