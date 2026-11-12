"""E464: Stop is honoured while the Yahoo download runs, and a worker's late progress write no longer undoes a Stop."""
from __future__ import annotations

import threading
import time

import data_feed as df
from test_data_feed import bhav_rows, feed, kv, store  # noqa: F401  (fixtures + helper)
import pytest


@pytest.fixture(autouse=True)
def _clean_stop_flag():
    """The Stop flag is a module-level threading.Event: these tests set it on purpose, so it must start and end clear (E469: it leaked
    from one test into the next, which only showed once the file was run with pytest for the first time)."""
    df.clear_data_feed_stop()
    yield
    df.clear_data_feed_stop()


# ── set_job: a progress write after Stop ─────────────────────────────────────

def test_progress_write_after_stop_keeps_stopping_status_and_message(store):
    store.set_job(status="stopping", message="Stop requested — finishing current symbol…", stop_requested=True)
    df.request_data_feed_stop()
    j = store.set_job(status="running", message="Bulk phase: POST /quotes/bulk…", processed=7, total=10)
    assert j["status"] == "stopping" and j["message"].startswith("Stop requested")
    assert j["processed"] == 7 and j["total"] == 10                       # the counters still move


def test_progress_write_after_a_finished_stop_keeps_stopped(store):
    store.set_job(status="stopped", message="done stopping")
    df.request_data_feed_stop()
    assert store.set_job(status="running", processed=3)["status"] == "stopped"


def test_without_the_stop_flag_running_still_wins(store):
    store.set_job(status="stopped", message="old")
    assert store.set_job(status="running", message="again", processed=1)["status"] == "running"


def test_a_start_write_with_started_at_is_never_blocked(store):
    store.set_job(status="stopped", message="old")
    df.request_data_feed_stop()                                           # stale flag from the earlier Stop
    j = store.set_job(status="running", message="new run", started_at="2026-11-11T09:15:00+05:30", processed=0)
    assert j["status"] == "running" and j["message"] == "new run"


def test_running_job_with_flag_set_is_not_touched(store):
    store.set_job(status="running", message="m1")
    df.request_data_feed_stop()
    j = store.set_job(status="running", message="m2", processed=2)
    assert j["status"] == "running" and j["message"] == "m2"


def test_non_running_writes_pass_through_while_stopping(store):
    store.set_job(status="stopping")
    df.request_data_feed_stop()
    j = store.set_job(status="stopped", message="Stopped after 5", processed=5)
    assert j["status"] == "stopped" and j["message"] == "Stopped after 5"


# ── the Yahoo phase of run_bulk_yahoo_price_feed ─────────────────────────────

def test_stop_during_the_yahoo_download_returns_promptly_and_writes_nothing(feed, monkeypatch):
    feed.open = True
    feed.bhav = bhav_rows("A")
    release = threading.Event()

    def slow_yahoo(targets):
        feed.yahoo_calls.append(list(targets))
        release.wait(10)                                                  # a download that would take a long time
        return {"A": {"symbol": "A", "price": 11.0}, "B": {"symbol": "B", "price": 22.0}}

    monkeypatch.setattr(df, "bulk_yahoo_download_prices", slow_yahoo)
    threading.Timer(0.15, df.request_data_feed_stop).start()
    t0 = time.time()
    try:
        r = df.run_bulk_yahoo_price_feed(["A", "B"])
    finally:
        release.set()                                                     # let the abandoned download thread end
    took = time.time() - t0
    assert took < 3.0
    assert r["status"] == "stopped" and r["tracked_stocks"] == 1 and r["requested"] == 2 and r["symbols"] == ["A"]
    assert "Yahoo phase" in r["message"]
    assert feed.store.get_symbol("B") is None                             # the dropped download was not written


def test_stop_set_as_the_download_finishes_drops_the_result(feed, monkeypatch):
    feed.open = True
    feed.bhav = bhav_rows("A")

    def yahoo_then_stop(targets):
        df.request_data_feed_stop()
        return {"B": {"symbol": "B", "price": 22.0}}

    monkeypatch.setattr(df, "bulk_yahoo_download_prices", yahoo_then_stop)
    r = df.run_bulk_yahoo_price_feed(["A", "B"])
    assert r["status"] == "stopped" and r["symbols"] == ["A"] and feed.store.get_symbol("B") is None


def test_without_a_stop_the_download_is_written_as_before(feed):
    feed.open = True
    feed.bhav = bhav_rows("A")
    feed.yahoo = {"B": {"symbol": "B", "price": 22.0}}
    r = df.run_bulk_yahoo_price_feed(["A", "B"])
    assert r["status"] == "success" and r["yahoo_hits"] == 1 and sorted(r["symbols"]) == ["A", "B"]


def test_a_download_error_still_takes_the_warning_path(feed):
    feed.open = True
    feed.bhav = bhav_rows("A")
    feed.yahoo_raises = RuntimeError("mds down")
    r = df.run_bulk_yahoo_price_feed(["A", "B"])
    assert r["status"] == "success" and r["yahoo_hits"] == 0 and r["symbols"] == ["A"]


def test_interruptible_helper_returns_prices_and_stop_flag(monkeypatch):
    monkeypatch.setattr(df, "bulk_yahoo_download_prices", lambda t: {"X": {"price": 1}})
    assert df._bulk_download_interruptible(["X"]) == ({"X": {"price": 1}}, False)

    def boom(t):
        raise ValueError("x")

    monkeypatch.setattr(df, "bulk_yahoo_download_prices", boom)
    try:
        df._bulk_download_interruptible(["X"])
        raised = False
    except ValueError:
        raised = True
    assert raised


# ── the cached wrapper: a stopped run is not a whole-universe cache write ────

def test_a_stopped_run_does_not_mark_the_bulk_cache_complete(feed, monkeypatch):
    feed.store.put_symbol("A", {"symbol": "A", "price": 10.0, "close": 10.0})
    monkeypatch.setattr(df, "should_refresh_bulk_cache", lambda: True)
    monkeypatch.setattr(df, "run_bulk_yahoo_price_feed", lambda syms, merge_existing=True: {"status": "stopped", "symbols": ["A"]})
    seen = {}
    monkeypatch.setattr(df, "set_bulk_quote_cache", lambda q, source=None, complete=True: seen.update(complete=complete, n=len(q)))
    df.run_bulk_yahoo_price_feed_cached(None)
    assert seen == {"complete": False, "n": 1}
    seen.clear()
    monkeypatch.setattr(df, "run_bulk_yahoo_price_feed", lambda syms, merge_existing=True: {"status": "success", "symbols": ["A"]})
    df.run_bulk_yahoo_price_feed_cached(None)
    assert seen == {"complete": True, "n": 1}
