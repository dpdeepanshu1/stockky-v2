"""group302: the per-symbol "Bhavcopy EOD waterfall hit" INFO flood is now DEBUG plus one INFO summary per window."""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


class _Cap(logging.Handler):
    def __init__(self, level=logging.INFO):
        super().__init__(level)
        self.msgs = []

    def emit(self, record):
        self.msgs.append((record.levelno, record.getMessage()))


def _run(fn, level=logging.INFO):
    h, old = _Cap(level), main.logger.level
    main.logger.addHandler(h)
    main.logger.setLevel(level)
    try:
        fn()
    finally:
        main.logger.removeHandler(h)
        main.logger.setLevel(old)
    return h.msgs


def _reset():
    main._BHAV_HIT_LOGGED.clear()
    main._BHAV_HIT_WINDOW.update({"since": 0.0, "hits": 0, "new": 0})


def test_default_logs_no_per_symbol_info_lines(monkeypatch):
    monkeypatch.delenv("BHAVCOPY_HIT_LOG_PER_SYMBOL", raising=False)
    _reset()

    def go():
        for i in range(300):
            main._log_bhavcopy_hit_once(f"SYM{i}", 10.0 + i)

    msgs = _run(go)
    assert [m for m in msgs if "waterfall hit" in m[1]] == []


def test_per_symbol_lines_are_available_at_debug(monkeypatch):
    monkeypatch.delenv("BHAVCOPY_HIT_LOG_PER_SYMBOL", raising=False)
    _reset()
    msgs = _run(lambda: main._log_bhavcopy_hit_once("ABC", 10.0), level=logging.DEBUG)
    assert any(lvl == logging.DEBUG and "waterfall hit ABC" in m for lvl, m in msgs)


def test_one_summary_after_the_window(monkeypatch):
    monkeypatch.delenv("BHAVCOPY_HIT_LOG_PER_SYMBOL", raising=False)
    monkeypatch.setenv("BHAVCOPY_HIT_SUMMARY_EVERY_S", "5")
    _reset()
    clock = {"t": 1000.0}
    monkeypatch.setattr(main.time, "time", lambda: clock["t"])

    def go():
        main._log_bhavcopy_hit_once("A", 1.0)
        main._log_bhavcopy_hit_once("A", 1.0)  # repeat, not new
        main._log_bhavcopy_hit_once("B", 2.0)
        clock["t"] += 6
        main._log_bhavcopy_hit_once("C", 3.0)  # window elapsed -> summary of 4 lookups, 3 new

    msgs = _run(go)
    summaries = [m for lvl, m in msgs if lvl == logging.INFO and "Bhavcopy EOD waterfall:" in m]
    assert len(summaries) == 1
    assert "4 lookups" in summaries[0] and "3 new prices" in summaries[0]
    assert main._BHAV_HIT_WINDOW["hits"] == 0


def test_env_switch_restores_group235_behaviour(monkeypatch):
    monkeypatch.setenv("BHAVCOPY_HIT_LOG_PER_SYMBOL", "1")
    _reset()

    def go():
        main._log_bhavcopy_hit_once("ABC", 10.0)
        main._log_bhavcopy_hit_once("ABC", 10.0)
        main._log_bhavcopy_hit_once("ABC", 11.0)

    msgs = _run(go)
    assert len([m for lvl, m in msgs if "hit ABC" in m]) == 2
