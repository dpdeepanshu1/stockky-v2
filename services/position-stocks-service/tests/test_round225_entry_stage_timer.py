"""round 225: attempt_entry's stage timer writes ONE warning for a slow call and nothing for a fast one (log only)."""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from orders import entry_stage_timer as t


def _set(monkeypatch, **kw):
    for k, v in kw.items():
        monkeypatch.setattr(config, k, v, raising=False)


def test_fast_call_logs_nothing(monkeypatch, caplog):
    _set(monkeypatch, ENTRY_STAGE_LOG=True, ENTRY_STAGE_SLOW_S=1.0, ENTRY_STAGE_MIN_S=0.1)
    t.begin("ABC")
    t.mark("a")
    with caplog.at_level(logging.WARNING, logger="position-stocks-entry-stage"):
        assert t.end() is None
    assert not caplog.records


def test_slow_call_lists_slow_stages_largest_first(monkeypatch, caplog):
    _set(monkeypatch, ENTRY_STAGE_LOG=True, ENTRY_STAGE_SLOW_S=0.05, ENTRY_STAGE_MIN_S=0.02)
    clock = iter([0.0, 0.0, 0.03, 0.33, 0.34, 0.35, 0.36])      # begin, mark a, mark b, mark c, ... end
    monkeypatch.setattr(t.time, "perf_counter", lambda: next(clock))
    t.begin("XYZ")          # t0 = 0.0, last = 0.0
    t.mark("a")             # 0.0 -> a 0.00 (clock 0.0)... consumed 0.0
    t.mark("b")             # b = 0.03
    t.mark("c")             # c = 0.30
    with caplog.at_level(logging.WARNING, logger="position-stocks-entry-stage"):
        line = t.end("ok")
    assert line and "XYZ" in line and "attempt_entry took" in line
    assert line.index("c 0.30s") < line.index("b 0.03s")
    assert "a 0.00s" not in line
    assert len(caplog.records) == 1


def test_format_line_when_no_stage_is_big():
    line = t.format_line("S", 2.0, [("a", 0.01)], 0.1, "ok")
    assert "no single stage reached the minimum" in line


def test_switch_off_and_zero_threshold(monkeypatch):
    _set(monkeypatch, ENTRY_STAGE_LOG=False)
    t.begin("A")
    t.mark("x")
    assert t.end() is None
    _set(monkeypatch, ENTRY_STAGE_LOG=True, ENTRY_STAGE_SLOW_S=0.0)
    t.begin("A")
    assert t.end() is None


def test_never_raises_without_begin():
    t.mark("x")
    assert t.end() is None
