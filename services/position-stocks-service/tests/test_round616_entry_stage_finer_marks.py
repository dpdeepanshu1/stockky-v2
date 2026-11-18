"""round 616: the 18 Nov slow skips (FISCHER / SBFC, 1.0-2.8 s) were ONE lump "skip_gate_logic". The gate work between the symbol claim and the
setup refusal now has its own stage marks (log only; no entry behaviour changes), so the next SLOW line names the step."""
from __future__ import annotations

import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(HERE, "orders", "entry.py"), encoding="utf-8").read()


def _body():
    return SRC[SRC.index("def _attempt_entry_impl"):SRC.index("def _correct_entry_fill_now")]


def test_finer_marks_exist_in_order_between_symbol_claim_and_early_gates():
    names = re.findall(r'entry_stage_timer\.mark\("([a-z_]+)"\)', _body())
    want = ["symbol_claim", "price_guard", "reentry_guard", "depth_gate", "flow_orb_odds", "pre_setup", "setup_quote", "setup_tape", "setup_inputs",
            "setup_classify", "early_gates"]
    pos = [names.index(n) for n in want]
    assert pos == sorted(pos), names


def test_marks_are_bare_statements_never_inside_a_condition_or_return():
    for line in _body().splitlines():
        if "entry_stage_timer.mark(" in line:
            assert line.strip().startswith("entry_stage_timer.mark(") and "return" not in line and " if " not in line, line


if __name__ == "__main__":
    test_finer_marks_exist_in_order_between_symbol_claim_and_early_gates()
    test_marks_are_bare_statements_never_inside_a_condition_or_return()
    print("ok")
