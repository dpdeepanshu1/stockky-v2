"""E285 round 159 (plan item 17): rows of a failed live_quotes write are sent ONCE more, in smaller batches.

Run from services/market-data-service:   python3 -m pytest tests/test_e285_round159_live_quotes_retry.py -v
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import angelone_ws_feed as f
import live_write_retry as lwr
from dhan_data import live_store as ls
from tests.test_group275_dhan_followups import _Engine, _rows  # noqa: F401


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("LIVE_QUOTES_RETRY_MAX_AGE_S", "LIVE_QUOTES_RETRY_CHUNK", "ANGELONE_FEED_DB_CHANGE_ONLY",
              "DHAN_LIVE_WRITE_REFRESH_S", "DHAN_LIVE_WRITE_BACKOFF_S"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DHAN_LIVE_WRITE", "on")
    monkeypatch.setenv("DHAN_LIVE_WRITE_REFRESH_S", "0")
    monkeypatch.setenv("DHAN_LIVE_WRITE_CHUNK", "100")
    f._RETRY.clear(); ls._RETRY.clear()
    f._AO_LAST_SENT.clear()
    with f._DB_PENDING_LOCK:
        f._DB_PENDING.clear()
    for k in list(ls._stats):
        ls._stats[k] = None if k == "last_error" else 0
    ls._last_sent.clear(); ls._latest.clear(); ls._prio.clear()
    ls._backoff_until = 0.0
    yield
    end = time.time() + 5
    while f._db_writer_busy and time.time() < end:
        time.sleep(0.02)
    f._RETRY.clear(); ls._RETRY.clear()
    f._AO_LAST_SENT.clear()
    ls._backoff_until = 0.0
    ls._prio.clear()


# ── the box itself ─────────────────────────────────────────────────────────────────────────────────────────────────
def _never(*a):
    return False


def test_box_returns_each_failed_row_once():
    b = lwr.RetryBox()
    b.add([("A", 1), ("B", 2)], now=100.0)
    due = b.take_due(_never, _never, now=101.0)
    assert sorted(due) == [("A", 1), ("B", 2)]
    assert b.take_due(_never, _never, now=101.5) == []           # taken: nothing is retried a second time


def test_box_drops_rows_older_than_the_max_age():
    b = lwr.RetryBox()
    b.add([("A", 1)], now=100.0)
    assert b.take_due(_never, _never, now=100.0 + 12.1) == []    # default 12 s
    assert b.stats["expired"] == 1 and len(b) == 0


def test_box_max_age_env_and_zero_turns_it_off(monkeypatch):
    monkeypatch.setenv("LIVE_QUOTES_RETRY_MAX_AGE_S", "30")
    b = lwr.RetryBox()
    b.add([("A", 1)], now=100.0)
    assert b.take_due(_never, _never, now=125.0) == [("A", 1)]
    monkeypatch.setenv("LIVE_QUOTES_RETRY_MAX_AGE_S", "0")
    assert b.add([("B", 2)]) == 0 and len(b) == 0
    monkeypatch.setenv("LIVE_QUOTES_RETRY_MAX_AGE_S", "oops")    # bad value -> the default
    assert lwr.max_age_s() == 12.0
    monkeypatch.setenv("LIVE_QUOTES_RETRY_CHUNK", "")
    assert lwr.chunk_size() == 10


def test_box_skips_superseded_rows_and_holds_the_ones_that_must_wait():
    b = lwr.RetryBox()
    b.add([("NEW", 1), ("WAIT", 2), ("GO", 3)], now=100.0)
    due = b.take_due(lambda k, ts: k == "NEW", lambda k: k == "WAIT", now=101.0)
    assert due == [("GO", 3)]
    assert b.stats["superseded"] == 1 and len(b) == 1             # WAIT stays for a later pass
    assert b.take_due(_never, _never, now=102.0) == [("WAIT", 2)]


def test_box_newer_failed_row_replaces_the_older_one_and_is_capped():
    b = lwr.RetryBox(cap=2)
    b.add([("A", 1)], now=100.0)
    b.add([("A", 9), ("B", 2), ("C", 3)], now=101.0)
    assert b.stats["overflow"] == 1
    assert dict(b.take_due(_never, _never, now=102.0)) == {"A": 9, "B": 2}


def test_box_survives_a_broken_callback():
    b = lwr.RetryBox()
    b.add([("A", 1)], now=100.0)

    def boom(*a):
        raise RuntimeError("x")

    assert b.take_due(boom, boom, now=101.0) == [("A", 1)]


# ── AngelOne background writer ─────────────────────────────────────────────────────────────────────────────────────
def _row(sym, ltp=100.0):
    return (sym, ltp, 1, 1, 1, 1, 1)


def _fake_writer(monkeypatch, results):
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


def test_angelone_failed_batch_is_retried_once_in_smaller_batches(monkeypatch):
    monkeypatch.setenv("LIVE_QUOTES_RETRY_CHUNK", "10")
    seen = _fake_writer(monkeypatch, [False, True, True, True])
    f._queue_db_rows([_row(f"S{i}", 100.0 + i) for i in range(25)])
    _wait_idle()
    assert seen[0] == [f"S{i}" for i in range(25)]                # the original 25-row write failed
    assert [len(x) for x in seen[1:]] == [10, 10, 5]              # retried once, 10 rows per transaction
    assert f._RETRY.snapshot()["written"] == 25 and f._RETRY.snapshot()["waiting"] == 0
    assert f._AO_LAST_SENT["S3"][0] == 103.0                      # the retry counts as sent


def test_angelone_a_retry_that_fails_is_not_tried_a_third_time(monkeypatch):
    seen = _fake_writer(monkeypatch, [False, False, True, True])
    f._queue_db_rows([_row("AAA"), _row("BBB")])
    _wait_idle()
    assert seen == [["AAA", "BBB"], ["AAA", "BBB"]]               # original + one retry, then nothing
    assert f._RETRY.snapshot()["failed"] == 2 and len(f._RETRY) == 0


def test_angelone_a_newer_write_supersedes_the_retry(monkeypatch):
    seen = _fake_writer(monkeypatch, [True])
    f._RETRY.add([("AAA", _row("AAA", 90.0))])
    f._AO_LAST_SENT["AAA"] = (95.0, time.monotonic() + 1)         # written successfully after the failure
    f._ao_retry_pass()
    assert seen == [] and f._RETRY.snapshot()["superseded"] == 1


def test_angelone_old_rows_are_not_retried(monkeypatch):
    seen = _fake_writer(monkeypatch, [True])
    f._RETRY.add([("AAA", _row("AAA"))], now=time.monotonic() - 60)
    f._ao_retry_pass()
    assert seen == [] and f._RETRY.snapshot()["expired"] == 1


def test_angelone_during_backoff_only_held_positions_are_retried(monkeypatch):
    seen = _fake_writer(monkeypatch, [True, True])
    ls.mark_priority(["HELD"])
    ls._backoff_until = time.monotonic() + 60
    f._RETRY.add([("HELD", _row("HELD")), ("OTHER", _row("OTHER"))])
    f._ao_retry_pass()
    assert seen == [["HELD"]] and len(f._RETRY) == 1              # OTHER waits
    ls._backoff_until = 0.0
    f._ao_retry_pass()
    assert seen == [["HELD"], ["OTHER"]]


def test_angelone_retry_off_keeps_the_old_behaviour(monkeypatch):
    monkeypatch.setenv("LIVE_QUOTES_RETRY_MAX_AGE_S", "0")
    seen = _fake_writer(monkeypatch, [False, True])
    f._queue_db_rows([_row("AAA")])
    _wait_idle()
    assert seen == [["AAA"]] and len(f._RETRY) == 0


def test_angelone_status_shows_the_retry_counters():
    assert {"waiting", "queued", "written", "failed", "expired", "superseded"} <= set(f.feed_status()["retry"])


# ── Dhan writer ────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture()
def store(monkeypatch):
    import angelone_ws_feed as live_feed         # looked up at run time, like live_store does (another test module may reload it)
    eng = _Engine()
    monkeypatch.setattr(live_feed, "_get_live_quotes_engine", lambda: (eng, "oracle"))
    monkeypatch.setattr(live_feed, "_ensure_schema", lambda e, d: None)
    monkeypatch.setenv("DHAN_LIVE_WRITE_BACKOFF_S", "0")          # the fake failure is a DPY-4024 timeout; most tests do not want the back-off
    return eng


def test_dhan_chunk_that_failed_for_good_is_retried_once_in_smaller_batches(store, monkeypatch):
    monkeypatch.setenv("LIVE_QUOTES_RETRY_CHUNK", "10")
    store.fail_on = {1}                                           # the 15-row chunk fails (too small to split)
    assert ls._write_sync(_rows(15)) == 0
    assert store.executed == [15] and len(ls._RETRY) == 15
    store.fail_on = set()
    assert ls.retry_failed() == 15
    assert store.executed == [15, 10, 5]                          # ten per transaction
    assert ls.retry_failed() == 0                                 # nothing left, nothing is sent twice
    assert ls.status()["retry"]["written"] == 15


def test_dhan_a_retry_that_fails_is_dropped(store):
    store.fail_on = {1, 2}
    ls._write_sync(_rows(5))
    assert ls.retry_failed() == 0
    assert store.executed == [5, 5]
    store.fail_on = set()
    assert ls.retry_failed() == 0 and store.executed == [5, 5]    # not a third try
    assert ls.status()["retry"]["failed"] == 5


def test_dhan_a_later_successful_write_supersedes_the_retry(store):
    store.fail_on = {1}
    ls._write_sync(_rows(5))
    store.fail_on = set()
    assert ls._write_sync(_rows(5)) == 5                          # the next poll wrote the same symbols
    n = len(store.executed)
    assert ls.retry_failed() == 0 and len(store.executed) == n
    assert ls.status()["retry"]["superseded"] == 5


def test_dhan_backoff_retries_only_held_positions(store, monkeypatch):
    monkeypatch.setenv("DHAN_LIVE_WRITE_BACKOFF_S", "20")
    store.fail_on = {1}
    ls._write_sync(_rows(4))                                      # failure is a DPY-4024 timeout -> back-off opens
    assert ls.in_backoff()
    store.fail_on = set()
    ls.mark_priority(["S1"])
    n = len(store.executed)
    assert ls.retry_failed() == 1 and store.executed[n:] == [1]
    ls._backoff_until = 0.0
    assert ls.retry_failed() == 3


def test_dhan_loop_sends_the_retry_right_after_the_failed_write(store):
    store.fail_on = {1}
    ls.submit(_rows(5))                                           # the write fails; the same loop pass sends the five rows once more
    end = time.time() + 5
    while time.time() < end and len(store.executed) < 2:
        time.sleep(0.02)
    assert store.executed == [5, 5]
    end = time.time() + 2
    while time.time() < end and len(ls._RETRY):
        time.sleep(0.02)
    assert len(ls._RETRY) == 0 and ls.status()["retry"]["written"] == 5


def test_dhan_sql_helper_keeps_both_dialects():
    assert ls._merge_sql("oracle").startswith("MERGE INTO live_quotes") and "'dhan'" in ls._merge_sql("oracle")
    assert "ON CONFLICT (symbol)" in ls._merge_sql("postgresql") and "'dhan'" in ls._merge_sql("postgresql")
