"""round 602: a slow SKIPPED attempt_entry now says which step was slow (log only; no entry behaviour changes).

Before: the DB-touching gates before the first tail mark (opening_protect, stopout_guard, entry_cap, open-position count, held-here query, symbol claim) and the
SKIPPED audit-row write were all unmarked, so a skip that took 1.4-5.2 s showed as one lump `after_last_mark` and the outcome read "ok".
Works under pytest and as a plain runner: python3 tests/test_round602_entry_stage_skip_marks.py"""
from __future__ import annotations

import logging
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import config
from orders import entry_stage_timer as t

SRC = open(os.path.join(HERE, "orders", "entry.py"), encoding="utf-8").read()


def _impl_body():
    return SRC[SRC.index("def _attempt_entry_impl"):SRC.index("def _correct_entry_fill_now")]


def test_new_marks_exist_in_order_before_the_old_ones():
    names = re.findall(r'entry_stage_timer\.mark\("([a-z_]+)"\)', _impl_body())
    want = ["gate_state", "opening_protect", "stopout_guard", "entry_cap", "open_count_query", "held_here_query", "symbol_claim", "early_gates"]
    pos = [names.index(n) for n in want]
    assert pos == sorted(pos), names


def test_log_candidate_splits_gate_logic_from_the_db_write_for_skips_only():
    wrapper = SRC[SRC.index("def _log_candidate("):SRC.index("def _log_candidate_impl(")]
    assert 'mark("skip_gate_logic")' in wrapper and 'mark("skip_log_write")' in wrapper
    assert 'decision == "SKIPPED"' in wrapper and "finally:" in wrapper
    assert "return _log_candidate_impl(" in wrapper


def test_attempt_entry_names_the_skip_reason():
    wrapper = SRC[SRC.index("def attempt_entry("):SRC.index("def _attempt_entry_impl(")]
    assert '"skip " + str(last_skip_reason()' in wrapper and "entry_stage_timer.end(_outcome)" in wrapper


def test_a_slow_skip_names_the_slow_step_and_the_reason():
    config.ENTRY_STAGE_LOG, config.ENTRY_STAGE_SLOW_S, config.ENTRY_STAGE_MIN_S = True, 1.0, 0.1
    clock = iter([0.0, 0.05, 0.10, 0.12, 3.20, 3.21])     # begin, gate_state, symbol_claim, skip_gate_logic, skip_log_write (3.08 s), end
    orig = t.time.perf_counter
    t.time.perf_counter = lambda: next(clock)
    try:
        t.begin("ABC")
        t.mark("gate_state")
        t.mark("symbol_claim")
        t.mark("skip_gate_logic")
        t.mark("skip_log_write")
        records = []

        class H(logging.Handler):
            def emit(self, rec):
                records.append(rec)
        lg = logging.getLogger("position-stocks-entry-stage")
        h = H()
        # Round 643: an earlier test in a full run can leave logging disabled or the level raised; set it here, restore below.
        saved_log = (lg.level, lg.disabled, logging.root.manager.disable)
        lg.setLevel(logging.WARNING)
        lg.disabled = False
        logging.disable(logging.NOTSET)
        lg.addHandler(h)
        try:
            line = t.end("skip SYMBOL_HELD_BY_OTHER_SERVICE")
        finally:
            lg.removeHandler(h)
            lg.setLevel(saved_log[0])
            lg.disabled = saved_log[1]
            logging.disable(saved_log[2])
    finally:
        t.time.perf_counter = orig
    assert line and "skip SYMBOL_HELD_BY_OTHER_SERVICE" in line and "skip_log_write 3.08s" in line
    assert "after_last_mark" not in line                     # the lump is gone: the slow step has its own name
    assert len(records) == 1


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1; print("pass", k)
    print(n, "passed")
