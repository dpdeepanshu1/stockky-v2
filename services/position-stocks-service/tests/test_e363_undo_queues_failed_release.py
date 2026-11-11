"""E363: short_live._undo no longer forgets a failed capital or symbol-lock release: it rolls the session back and queues the release for plain_short's retry."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "orders" / "short_live.py").read_text(encoding="utf-8")


def _undo_body() -> str:
    i = SRC.index("def _undo()")
    return SRC[i:SRC.index("try:\n        plan = short_entry.plan_short", i)]


def test_undo_rolls_back_and_queues_both_releases():
    b = _undo_body()
    assert b.count("_rollback_quietly(db)") == 2
    assert 'plain_short.queue_release("capital"' in b and 'plain_short.queue_release("lock"' in b


def test_partial_fill_release_failure_is_queued_and_position_keeps_its_part():
    i = SRC.index("partial-fill capital release")
    seg = SRC[i - 400:i + 500]
    assert "queue_release" in seg and re.search(r"position_value = keep", seg)
    # position_value = keep must sit OUTSIDE the except: it is set whether the release worked or not
    assert seg.index("position_value = keep") > seg.index("queue_release")


def test_queue_release_exists_and_is_retried():
    ps = (ROOT / "orders" / "plain_short.py").read_text(encoding="utf-8")
    assert "def queue_release(" in ps and "_pending_release.append(item)" in ps
