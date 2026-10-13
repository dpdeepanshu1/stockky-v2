"""
group346 (log review #12): the AngelOne poll cycle no longer waits for each batch's live_quotes write.

The in-memory _LIVE cache is already current when a batch returns; the durable copy goes to one background writer that
keeps the latest row per symbol and writes <= 50 rows per transaction.

Run from services/market-data-service:
    python3 -m pytest tests/test_group346_angelone_feed_async_db.py -q
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import angelone_ws_feed as f


def _rows(prefix, n, ltp):
    return [(f"{prefix}{i}", ltp, 1, 1, 1, 1, 1) for i in range(n)]


def _wait_idle(timeout=5.0):
    end = time.time() + timeout
    while f._db_writer_busy and time.time() < end:
        time.sleep(0.02)
    assert not f._db_writer_busy


@pytest.fixture(autouse=True)
def _clean():
    with f._DB_PENDING_LOCK:
        f._DB_PENDING.clear()
    yield
    _wait_idle()


def test_queueing_does_not_wait_for_the_database(monkeypatch):
    written = []

    def slow(rows):
        time.sleep(0.05)
        written.append([r[0] for r in rows])

    monkeypatch.setattr(f, "_upsert_ticks_batch_sync", slow)
    t0 = time.perf_counter()
    for i in range(3):
        f._queue_db_rows(_rows("S", 120, 100 + i))
    assert time.perf_counter() - t0 < 0.1
    _wait_idle()
    syms = [s for b in written for s in b]
    assert set(syms) == {f"S{i}" for i in range(120)}
    assert all(len(b) <= f.BATCH_SIZE for b in written)


def test_latest_row_per_symbol_wins(monkeypatch):
    seen = {}
    release = []

    def writer(rows):
        while not release:
            time.sleep(0.01)
        for r in rows:
            seen[r[0]] = r[1]

    monkeypatch.setattr(f, "_upsert_ticks_batch_sync", writer)
    f._queue_db_rows([("AAA", 1, 1, 1, 1, 1, 1)])
    time.sleep(0.05)                       # writer has taken AAA@1 and is blocked
    f._queue_db_rows([("BBB", 5, 1, 1, 1, 1, 1)])
    f._queue_db_rows([("BBB", 6, 1, 1, 1, 1, 1)])
    release.append(1)
    _wait_idle()
    assert seen["BBB"] == 6


def test_rows_without_symbol_are_ignored(monkeypatch):
    monkeypatch.setattr(f, "_upsert_ticks_batch_sync", lambda rows: None)
    f._queue_db_rows([None, ("", 1, 1, 1, 1, 1, 1)])
    assert not f._DB_PENDING


def test_feed_status_reports_the_writer():
    st = f.feed_status()
    assert "db_async" in st and "pending_rows" in st["db_async"]
