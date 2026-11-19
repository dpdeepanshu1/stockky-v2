"""Round 649 (E675): a slow attempt_entry call that ended in a SKIP logs at INFO below ENTRY_STAGE_SLOW_SKIP_S (default 5 s) and
WARNING from there; an entry / error / any non-skip outcome keeps the WARNING at ENTRY_STAGE_SLOW_S. Plain functions with their own
handler and a fake clock (no caplog), so an earlier test that left logging disabled cannot hide the records; `python3 <file>` runs them."""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import config
from orders import entry_stage_timer as t


def _run(outcome, total_s, skip_limit=5.0):
    """One call lasting total_s seconds; returns (returned line, [(levelname, message)])."""
    saved_cfg = (getattr(config, "ENTRY_STAGE_LOG", True), getattr(config, "ENTRY_STAGE_SLOW_S", 1.0),
                 getattr(config, "ENTRY_STAGE_MIN_S", 0.1), getattr(config, "ENTRY_STAGE_SLOW_SKIP_S", 5.0))
    config.ENTRY_STAGE_LOG, config.ENTRY_STAGE_SLOW_S, config.ENTRY_STAGE_MIN_S, config.ENTRY_STAGE_SLOW_SKIP_S = True, 1.0, 0.1, skip_limit
    records = []

    class H(logging.Handler):
        def emit(self, rec):
            records.append((rec.levelname, rec.getMessage()))

    lg = logging.getLogger("position-stocks-entry-stage")
    h = H()
    saved_log = (lg.level, lg.disabled, logging.root.manager.disable)
    lg.setLevel(logging.DEBUG)
    lg.disabled = False
    logging.disable(logging.NOTSET)
    lg.addHandler(h)
    clock = iter([0.0, total_s - 0.5, total_s])          # begin, depth_gate mark, end
    orig = t.time.perf_counter
    t.time.perf_counter = lambda: next(clock)
    try:
        t.begin("ABC")
        t.mark("depth_gate")
        line = t.end(outcome)
    finally:
        t.time.perf_counter = orig
        lg.removeHandler(h)
        lg.setLevel(saved_log[0])
        lg.disabled = saved_log[1]
        logging.disable(saved_log[2])
        (config.ENTRY_STAGE_LOG, config.ENTRY_STAGE_SLOW_S, config.ENTRY_STAGE_MIN_S, config.ENTRY_STAGE_SLOW_SKIP_S) = saved_cfg
    return line, records


def test_a_slow_skip_under_the_limit_is_info():
    line, recs = _run("skip WINDOW_NO_SETUP: only a setup", 3.6)
    assert line and "depth_gate" in line
    assert [r[0] for r in recs] == ["INFO"] and "entry SLOW: ABC" in recs[0][1]


def test_a_skip_at_or_over_the_limit_is_a_warning():
    line, recs = _run("skip SOFT_PENALTIES_TOO_HIGH", 6.48)
    assert line and [r[0] for r in recs] == ["WARNING"]


def test_a_slow_entry_is_still_a_warning():
    for outcome in ("ok", "OperationalError", "entered"):
        line, recs = _run(outcome, 1.5)
        assert line and [r[0] for r in recs] == ["WARNING"], outcome


def test_a_fast_skip_logs_nothing():
    line, recs = _run("skip WINDOW_NO_SETUP", 0.6)
    assert line is None and recs == []


def test_limit_zero_makes_every_slow_skip_a_warning_again():
    line, recs = _run("skip WINDOW_NO_SETUP", 2.0, skip_limit=0.0)
    assert [r[0] for r in recs] == ["WARNING"]


def test_the_wrapper_still_passes_the_skip_outcome_to_the_timer():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders", "entry.py")).read()
    wrapper = src[src.index("def attempt_entry("):src.index("def _attempt_entry_impl(")]
    assert '_outcome = "skip " + str(last_skip_reason()' in wrapper and "entry_stage_timer.end(_outcome)" in wrapper


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1; print("pass", k)
    print(n, "passed")
