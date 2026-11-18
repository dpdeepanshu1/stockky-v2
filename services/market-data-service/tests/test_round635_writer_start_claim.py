"""Round 635 (E661): a live_quotes writer thread that cannot start no longer leaves the writer "busy" for ever.

`_queue_db_rows` sets `_db_writer_busy = True` under the lock and only then starts the writer thread; only that thread clears the
flag. If `Thread.start()` raised ("can't start new thread" when the container is short of threads or memory) the flag stayed True,
so no later batch ever started a writer and live_quotes stopped being written until the service restarted. The claim is now given
back when the start fails. Run from services/market-data-service:
    python3 -m pytest tests/test_round635_writer_start_claim.py -q
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import angelone_ws_feed as f


def _row(sym, ltp=100.0):
    return (sym, ltp, 1, 1, 1, 1, 1)


def _wait_idle(timeout=5.0):
    end = time.time() + timeout
    while f._db_writer_busy and time.time() < end:
        time.sleep(0.02)
    assert not f._db_writer_busy


@pytest.fixture(autouse=True)
def _clean():
    with f._DB_PENDING_LOCK:
        f._DB_PENDING.clear()
        f._AO_LAST_SENT.clear()         # an earlier test's identical row would otherwise be skipped as "unchanged"
        f._db_writer_busy = False
    yield
    _wait_idle()
    with f._DB_PENDING_LOCK:
        f._DB_PENDING.clear()


class _NoThread:
    """Stand-in for threading.Thread whose start() fails, like a host that cannot create another thread."""

    def __init__(self, *a, **k):
        pass

    def start(self):
        raise RuntimeError("can't start new thread")


def test_failed_thread_start_gives_the_claim_back_and_keeps_the_rows(monkeypatch, caplog):
    monkeypatch.setattr(f.threading, "Thread", _NoThread)
    with caplog.at_level("WARNING"):
        f._queue_db_rows([_row("AAA"), _row("BBB")])      # must not raise into the poll loop
    assert f._db_writer_busy is False
    assert set(f._DB_PENDING) == {"AAA", "BBB"}
    assert any("could not start the thread" in r.getMessage() for r in caplog.records)


def test_the_next_batch_starts_a_writer_and_drains_everything(monkeypatch):
    written = []
    monkeypatch.setattr(f, "_upsert_ticks_batch_sync", lambda rows: written.append([r[0] for r in rows]) or True)
    real_thread = threading.Thread
    monkeypatch.setattr(f.threading, "Thread", _NoThread)
    f._queue_db_rows([_row("AAA")])                         # the start fails, the row stays queued
    assert f._db_writer_busy is False
    monkeypatch.setattr(f.threading, "Thread", real_thread)
    f._queue_db_rows([_row("BBB")])                         # a healthy start picks up both rows
    _wait_idle()
    assert {s for b in written for s in b} == {"AAA", "BBB"}
    assert f._DB_PENDING == {}


def test_a_normal_start_still_claims_and_releases(monkeypatch):
    written = []
    monkeypatch.setattr(f, "_upsert_ticks_batch_sync", lambda rows: written.append(len(rows)) or True)
    f._queue_db_rows([_row("AAA")])
    _wait_idle()
    assert sum(written) == 1
    assert f._db_writer_busy is False
