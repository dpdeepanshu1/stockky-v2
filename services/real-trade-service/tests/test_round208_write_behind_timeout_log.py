"""Round 208: a write-behind batch that times out is kept (never dropped), counted in requeued_timeout and logged once per minute with an accurate message."""
from __future__ import annotations

import logging

import write_behind as wb
from tests.test_a3_write_behind import _setup, _submit_no_worker


def _boom_timeout(db, rows):
    raise RuntimeError("(oracledb.exceptions.DatabaseError) DPY-4024: call timeout of 8000 ms exceeded")


def test_timeout_keeps_the_rows_and_counts_them(monkeypatch):
    _setup(monkeypatch, _boom_timeout, WRITE_BEHIND_BACKOFF_S=30)
    for i in range(3):
        _submit_no_worker("k", {"i": i})
    assert wb.drain_once() == 0
    st = wb.status()
    assert st["queued"] == 3 and st["requeued_timeout"] == 3 and st["dropped_failed"] == 0 and st["timeouts"] == 1


def test_timeout_line_says_kept_not_failed_and_is_throttled(monkeypatch, caplog):
    _setup(monkeypatch, _boom_timeout, WRITE_BEHIND_BACKOFF_S=0)          # no back-off: every drain_once() hits the handler again
    _submit_no_worker("k", {"i": 1})
    with caplog.at_level(logging.WARNING, logger=wb.logger.name):
        for _ in range(4):
            wb.drain_once()
    lines = [r.getMessage() for r in caplog.records if "write-behind" in r.getMessage()]
    assert len(lines) == 1 and "kept" in lines[0] and "failed" not in lines[0]
    assert wb.status()["requeued_timeout"] == 4


def test_timeout_line_is_logged_again_after_the_window_with_the_count(monkeypatch, caplog):
    _setup(monkeypatch, _boom_timeout, WRITE_BEHIND_BACKOFF_S=0)
    _submit_no_worker("k", {"i": 1})
    with caplog.at_level(logging.WARNING, logger=wb.logger.name):
        wb.drain_once()
        wb.drain_once()
        wb.drain_once()
        monkeypatch.setattr(wb, "_last_timeout_log", wb._last_timeout_log - 61.0)
        wb.drain_once()
    lines = [r.getMessage() for r in caplog.records if "write-behind" in r.getMessage()]
    assert len(lines) == 2 and "3 timeout(s) since the last line" in lines[1]


def test_a_real_failure_still_logs_failed_and_is_dropped_after_three_tries(monkeypatch, caplog):
    def bad(db, rows):
        raise ValueError("bad row")
    _setup(monkeypatch, bad)
    _submit_no_worker("k", {"i": 1})
    with caplog.at_level(logging.WARNING, logger=wb.logger.name):
        for _ in range(3):
            wb.drain_once()
    st = wb.status()
    assert st["dropped_failed"] == 1 and st["requeued_timeout"] == 0 and st["queued"] == 0
    assert sum("failed (" in r.getMessage() for r in caplog.records) == 3


def test_reset_clears_the_new_state(monkeypatch):
    _setup(monkeypatch, _boom_timeout)
    _submit_no_worker("k", {"i": 1})
    wb.drain_once()
    wb.reset_for_tests()
    assert wb.status()["requeued_timeout"] == 0 and wb._last_timeout_log == 0.0 and wb._timeouts_since_log == 0
