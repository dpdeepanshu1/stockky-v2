"""group356 (14 Oct 10:38 IST boot log: 'live_quotes MERGE ... 8 s call timeout, 444 of 474 rows written'): the AngelOne feed's background
live_quotes writer skips rows whose price has not changed, and shares the Dhan writer's Oracle back-off after a timeout."""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import angelone_ws_feed as f
from dhan_data import live_store as ls


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("ANGELONE_FEED_DB_CHANGE_ONLY", raising=False)
    monkeypatch.delenv("DHAN_LIVE_WRITE_REFRESH_S", raising=False)
    monkeypatch.delenv("DHAN_LIVE_WRITE_BACKOFF_S", raising=False)
    f._AO_LAST_SENT.clear()
    ls._backoff_until = 0.0
    ls._prio.clear()
    with f._DB_PENDING_LOCK:
        f._DB_PENDING.clear()
    yield
    end = time.time() + 5
    while f._db_writer_busy and time.time() < end:
        time.sleep(0.02)
    f._AO_LAST_SENT.clear()
    ls._backoff_until = 0.0
    ls._prio.clear()


def _row(sym, ltp=100.0):
    return (sym, ltp, 1, 1, 1, 1, 1)


def _drain_with(monkeypatch, results):
    """Queue each batch in `results` order through the real drain with a fake writer; returns the batches the writer saw."""
    seen = []
    it = iter(results)

    def writer(rows):
        seen.append([r[0] for r in rows])
        return next(it, True)

    monkeypatch.setattr(f, "_upsert_ticks_batch_sync", writer)
    return seen


def _wait_idle():
    end = time.time() + 5
    while f._db_writer_busy and time.time() < end:
        time.sleep(0.02)
    assert not f._db_writer_busy


def test_unchanged_price_is_not_written_again(monkeypatch):
    seen = _drain_with(monkeypatch, [True, True, True])
    f._queue_db_rows([_row("AAA", 100.0), _row("BBB", 50.0)])
    _wait_idle()
    f._queue_db_rows([_row("AAA", 100.0), _row("BBB", 51.0)])       # AAA unchanged, BBB moved
    _wait_idle()
    assert seen == [["AAA", "BBB"], ["BBB"]]
    assert f._db_async_stats["unchanged_skipped"] >= 1


def test_unchanged_price_is_written_again_after_the_refresh_window(monkeypatch):
    monkeypatch.setenv("DHAN_LIVE_WRITE_REFRESH_S", "0.05")
    seen = _drain_with(monkeypatch, [True, True])
    f._queue_db_rows([_row("AAA", 100.0)])
    _wait_idle()
    time.sleep(0.08)
    f._queue_db_rows([_row("AAA", 100.0)])
    _wait_idle()
    assert seen == [["AAA"], ["AAA"]]


def test_a_failed_write_is_retried_not_skipped_as_unchanged(monkeypatch):
    seen = _drain_with(monkeypatch, [False, True])
    f._queue_db_rows([_row("AAA", 100.0)])
    _wait_idle()
    f._queue_db_rows([_row("AAA", 100.0)])                          # same price, but the first write failed
    _wait_idle()
    assert seen == [["AAA"], ["AAA"]]


def test_backoff_writes_only_held_positions(monkeypatch):
    ls._backoff_until = time.monotonic() + 60
    ls.mark_priority(["HELD"])
    seen = _drain_with(monkeypatch, [True])
    f._queue_db_rows([_row("HELD", 10.0), _row("OTHER", 20.0)])
    _wait_idle()
    assert seen == [["HELD"]]
    assert f._db_async_stats["backoff_skipped"] >= 1


def test_priority_rows_are_never_skipped_as_unchanged(monkeypatch):
    ls.mark_priority(["HELD"])
    seen = _drain_with(monkeypatch, [True, True])
    f._queue_db_rows([_row("HELD", 10.0)])
    _wait_idle()
    f._queue_db_rows([_row("HELD", 10.0)])
    _wait_idle()
    assert seen == [["HELD"], ["HELD"]]


def test_switch_off_writes_everything_like_before(monkeypatch):
    monkeypatch.setenv("ANGELONE_FEED_DB_CHANGE_ONLY", "0")
    seen = _drain_with(monkeypatch, [True, True])
    f._queue_db_rows([_row("AAA", 100.0)])
    _wait_idle()
    f._queue_db_rows([_row("AAA", 100.0)])
    _wait_idle()
    assert seen == [["AAA"], ["AAA"]]


def test_note_timeout_opens_the_shared_backoff():
    assert not ls.in_backoff()
    ls.note_timeout()
    assert ls.in_backoff()


def test_batch_writer_returns_false_on_failure_and_opens_backoff_on_a_timeout(monkeypatch):
    class _Conn:
        def execute(self, *a, **k):
            raise RuntimeError("DPY-4024: call timeout of 8000 ms exceeded")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Eng:
        def begin(self):
            return _Conn()

    monkeypatch.setattr(f, "_get_live_quotes_engine", lambda: (_Eng(), "postgresql"))
    monkeypatch.setattr(f, "_ensure_schema", lambda *a: None)
    assert f._upsert_ticks_batch_sync([_row("AAA")]) is False
    assert ls.in_backoff()
