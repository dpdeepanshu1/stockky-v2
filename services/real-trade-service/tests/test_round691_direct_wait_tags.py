"""Round 691 (E713): WAIT rows that set decision.action = "WAIT" directly (not through _wait) now carry a gate_tag too. Source level, no database."""
from __future__ import annotations

import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(HERE, "entry_engine", "entry.py"), encoding="utf-8").read()
ROOT = os.path.dirname(os.path.dirname(HERE))


def test_duplicate_staged_wait_is_tagged():
    i = SRC.index("refusing a second BUY for the same")
    assert 'decision.gate_tag = "dup_staged"' in SRC[i:i + 700]


def test_composite_floor_and_top_n_waits_are_tagged():
    i = SRC.index("def _mark_skipped_wait")
    seg = SRC[i:i + 3500]
    for tag in ("below_floor", "not_top_n", "auto_pause_reserve"):
        assert f'"{tag}"' in seg, tag
    assert seg.index("decision.reasoning +=") < seg.index("decision.gate_tag") < seg.index("db.add(decision)")


def test_tags_fit_the_column_and_the_daily_read_lists_the_judgement_ones():
    assert all(len(t) <= 32 for t in ("dup_staged", "below_floor", "not_top_n", "auto_pause_reserve"))
    line = [ln for ln in open(os.path.join(ROOT, "scripts", "gates_daily.sh"), encoding="utf-8").read().splitlines() if ln.startswith("GATES=")][0]
    assert "below_floor" in line and "not_top_n" in line and "dup_staged" not in line
