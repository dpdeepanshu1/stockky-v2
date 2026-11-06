"""round 227: attempt_entry's tail (after reserve_capital) is split into stages so a slow entry names its stage (log only)."""
import os
import re

_SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders", "entry.py"), encoding="utf-8").read()


def test_tail_stages_are_marked_in_order():
    body = _SRC[_SRC.index("def _attempt_entry_impl"):_SRC.index("def _correct_entry_fill_now")]
    names = re.findall(r'entry_stage_timer\.mark\("([a-z_]+)"\)', body)
    want = ["reserve_capital", "security_id", "sizing", "cost_checks", "heat_gate", "order_budget", "live_anchor",
            "dhan_order_call", "gate_commit", "position_commit", "log_candidate_entered", "notify_and_approve", "fill_correction"]
    pos = [names.index(n) for n in want]
    assert pos == sorted(pos)
