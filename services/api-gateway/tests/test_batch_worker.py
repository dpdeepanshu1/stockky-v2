"""
tests/test_batch_worker.py — api-gateway/batch_worker.py (100%).

run_in_batches() is the shared scan driver: ordered chunks of `batch_size`, optional per-item
result cache (looked up / stored on worker threads), cancel checks, progress callbacks, and a
gc.collect() per batch.

Hermetic: no network, no sleeping beyond event-loop yields, no external services.
  * async code   -> driven with asyncio.run() from plain sync tests (no pytest-asyncio needed, so
                    the suite still works with only the packages in requirements-test.txt)
  * gc.collect   -> counted through a monkeypatched `bw.gc`
  * time         -> `start_time` is passed in and time.time is pinned where elapsed_sec matters
  * cache        -> plain dict-backed callbacks (they run on real worker threads via to_thread)

    cd services/api-gateway
    python -m pytest tests/test_batch_worker.py -v --cov=batch_worker --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import batch_worker as bw


# ─────────────────────────────── helpers ───────────────────────────────

def run(coro):
    return asyncio.run(coro)


async def echo(item):
    await asyncio.sleep(0)
    return item


def upper(item):
    async def _w(i=item):
        await asyncio.sleep(0)
        return str(i).upper()
    return _w()


@pytest.fixture
def gc_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(bw, "gc", types.SimpleNamespace(collect=lambda: calls.append(1)))
    return calls


class Recorder:
    """Async progress callback that records BatchProgress objects."""

    def __init__(self, raises=None):
        self.seen = []
        self.raises = raises

    async def __call__(self, progress):
        self.seen.append(progress)
        if self.raises:
            raise self.raises


# ─────────────────────────────── dataclasses / helpers ───────────────────────────────

class TestDataclasses:
    def test_batch_progress_defaults(self):
        p = bw.BatchProgress(total=10, processed=4, batch_index=1, batch_count=3, elapsed_sec=1.5)
        assert p.cancelled is False
        assert p.cache_hits == 0 and p.cache_misses == 0

    def test_batch_result_defaults_are_independent(self):
        a, b = bw.BatchResult(), bw.BatchResult()
        a.results.append(1)
        a.errors.append({"x": 1})
        assert b.results == [] and b.errors == []
        assert (a.processed, a.cancelled, a.cache_hits, a.cache_misses) == (0, False, 0, 0)


class TestDefaultBatchSize:
    @pytest.mark.parametrize("workers,minimum,expected", [
        (10, 6, 20),   # 2x workers
        (2, 6, 6),     # minimum wins
        (3, 6, 6),     # 2x == minimum
        (1, 1, 2),
        (0, 6, 6),
        (5, 12, 12),
    ])
    def test_values(self, workers, minimum, expected):
        assert bw.default_batch_size(workers, minimum) == expected

    def test_default_minimum_is_six(self):
        assert bw.default_batch_size(1) == 6

    def test_coerces_numeric_strings(self):
        assert bw.default_batch_size("8", "3") == 16


class TestCancelTasks:
    def test_cancels_pending_tasks(self):
        async def main():
            started = asyncio.Event()

            async def forever():
                started.set()
                await asyncio.sleep(3600)

            tasks = [asyncio.create_task(forever()) for _ in range(3)]
            await started.wait()
            await bw._cancel_tasks(tasks)
            return [t.cancelled() for t in tasks]

        assert run(main()) == [True, True, True]

    def test_skips_tasks_that_are_already_done(self):
        async def main():
            t = asyncio.create_task(echo(7))
            await t
            await bw._cancel_tasks([t])
            return t.cancelled(), t.result()

        assert run(main()) == (False, 7)

    def test_empty_list_is_a_noop(self):
        run(bw._cancel_tasks([]))

    def test_wait_errors_are_swallowed(self, monkeypatch):
        async def boom(*a, **k):
            raise RuntimeError("wait failed")

        async def main():
            t = asyncio.create_task(asyncio.sleep(3600))
            monkeypatch.setattr(bw.asyncio, "wait", boom)
            try:
                await bw._cancel_tasks([t])          # must not raise
            finally:
                monkeypatch.undo()
                await asyncio.gather(t, return_exceptions=True)
            return t.cancelled()

        assert run(main()) is True

    def test_timeout_is_forwarded_to_wait(self, monkeypatch):
        seen = {}
        real_wait = asyncio.wait

        async def spy(tasks, timeout=None):
            seen["timeout"] = timeout
            return await real_wait(tasks, timeout=timeout)

        async def main():
            t = asyncio.create_task(asyncio.sleep(3600))
            monkeypatch.setattr(bw.asyncio, "wait", spy)
            try:
                await bw._cancel_tasks([t], timeout=0.5)
            finally:
                monkeypatch.undo()

        run(main())
        assert seen["timeout"] == 0.5


# ─────────────────────────────── basic flow ───────────────────────────────

class TestEmptyAndBasic:
    def test_empty_input_returns_empty_result_without_calling_anything(self, gc_calls):
        called = []

        async def worker(i):
            called.append(i)

        out = run(bw.run_in_batches([], worker, cache_get=lambda i: called.append(i)))
        assert out.results == [] and out.errors == [] and out.processed == 0
        assert out.cancelled is False
        assert called == [] and gc_calls == []

    def test_processes_everything_in_order(self, gc_calls):
        out = run(bw.run_in_batches(["a", "b", "c", "d", "e"], echo, batch_size=2))
        assert out.results == ["a", "b", "c", "d", "e"]
        assert out.processed == 5
        assert out.errors == [] and out.cancelled is False
        assert out.cache_hits == 0 and out.cache_misses == 5

    def test_accepts_any_sequence(self, gc_calls):
        out = run(bw.run_in_batches(("x", "y"), echo, batch_size=5))
        assert out.results == ["x", "y"]

    def test_default_batch_size_is_twelve(self, gc_calls):
        rec = Recorder()
        run(bw.run_in_batches(list(range(25)), echo, on_progress=rec))
        assert [p.batch_count for p in rec.seen] == [3, 3, 3]
        assert [p.processed for p in rec.seen] == [12, 24, 25]

    @pytest.mark.parametrize("bs,expected_batches", [(1, 5), (2, 3), (5, 1), (50, 1), (0, 5), (-3, 5), ("2", 3)])
    def test_batch_size_is_clamped_and_coerced(self, gc_calls, bs, expected_batches):
        rec = Recorder()
        out = run(bw.run_in_batches(list(range(5)), echo, batch_size=bs, on_progress=rec))
        assert len(rec.seen) == expected_batches
        assert rec.seen[0].batch_count == expected_batches
        assert out.processed == 5

    def test_never_shrinks_the_work_list(self, gc_calls):
        items = list(range(101))
        out = run(bw.run_in_batches(items, echo, batch_size=7))
        assert out.results == items and out.processed == 101

    def test_in_flight_work_is_bounded_by_batch_size(self, gc_calls):
        state = {"now": 0, "peak": 0}

        async def worker(i):
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            await asyncio.sleep(0)
            state["now"] -= 1
            return i

        run(bw.run_in_batches(list(range(20)), worker, batch_size=4))
        assert state["peak"] == 4

    def test_items_within_a_batch_run_concurrently(self, gc_calls):
        async def main():
            gate = asyncio.Event()
            arrived = []

            async def worker(i):
                arrived.append(i)
                if len(arrived) == 3:
                    gate.set()
                await asyncio.wait_for(gate.wait(), timeout=2)   # deadlocks unless all 3 run together
                return i

            return await bw.run_in_batches([1, 2, 3], worker, batch_size=3)

        assert run(main()).results == [1, 2, 3]

    def test_logs_start_and_done(self, gc_calls, caplog):
        with caplog.at_level(logging.INFO, logger="batch-worker"):
            run(bw.run_in_batches(["a", "b"], echo, batch_size=1))
        msgs = [r.getMessage() for r in caplog.records]
        assert any(m.startswith("batch_worker start items=2 batch_size=1 batches=2 cache=False") for m in msgs)
        assert any(m.startswith("batch_worker done processed=2 results=2 errors=0") for m in msgs)


# ─────────────────────────────── errors / classification ───────────────────────────────

class TestErrors:
    def test_worker_exception_becomes_error_entry(self, gc_calls, caplog):
        async def worker(i):
            if i == "bad":
                raise ValueError("boom " + "x" * 400)
            return i

        with caplog.at_level(logging.ERROR, logger="batch-worker"):
            out = run(bw.run_in_batches(["ok", "bad", "ok2"], worker, batch_size=3))
        assert out.results == ["ok", "ok2"]
        assert out.processed == 3
        (err,) = out.errors
        assert err["item"] == "bad"
        assert err["error"].startswith("ValueError: boom ")
        assert len(err["error"]) == len("ValueError: ") + 160          # message truncated to 160 chars
        assert any("batch item failed bad" in r.getMessage() for r in caplog.records)

    def test_exceptions_not_collected_when_disabled(self, gc_calls):
        async def worker(i):
            raise RuntimeError("nope")

        out = run(bw.run_in_batches([1, 2], worker, collect_errors_from_exceptions=False))
        assert out.errors == [] and out.results == []
        assert out.processed == 2                                       # still counted

    def test_item_is_stringified_in_error(self, gc_calls):
        async def worker(i):
            raise KeyError("k")

        out = run(bw.run_in_batches([("RELIANCE", 1)], worker))
        assert out.errors[0]["item"] == "('RELIANCE', 1)"
        assert out.errors[0]["error"].startswith("KeyError:")

    def test_worker_cancellation_is_skipped_but_counted(self, gc_calls):
        async def worker(i):
            if i == 2:
                raise asyncio.CancelledError()
            return i

        out = run(bw.run_in_batches([1, 2, 3], worker, batch_size=3))
        assert out.results == [1, 3]
        assert out.errors == []                                         # cancellation is not an error
        assert out.processed == 3

    def test_classify_result_routes_to_errors(self, gc_calls):
        def classify(r):
            return {"item": r, "error": "negative"} if r < 0 else None

        out = run(bw.run_in_batches([1, -2, 3, -4], echo, batch_size=2, classify_result=classify))
        assert out.results == [1, 3]
        assert out.errors == [{"item": -2, "error": "negative"}, {"item": -4, "error": "negative"}]
        assert out.processed == 4

    def test_falsy_but_not_none_classification_still_counts_as_error(self, gc_calls):
        out = run(bw.run_in_batches([1], echo, classify_result=lambda r: {}))
        assert out.results == [] and out.errors == [{}]

    def test_classify_only_sees_successful_results(self, gc_calls):
        seen = []

        async def worker(i):
            if i == 1:
                raise RuntimeError("x")
            return i

        run(bw.run_in_batches([0, 1, 2], worker, classify_result=lambda r: seen.append(r)))
        assert seen == [0, 2]

    def test_classified_errors_are_not_cached(self, gc_calls):
        stored = []
        out = run(bw.run_in_batches(
            [1, -1], echo,
            classify_result=lambda r: {"e": r} if r < 0 else None,
            cache_set=lambda item, r: stored.append((item, r)),
        ))
        assert stored == [(1, 1)]
        assert out.errors == [{"e": -1}]

    def test_gather_failure_cancels_tasks_and_moves_on(self, gc_calls, monkeypatch, caplog):
        real_gather = asyncio.gather
        state = {"raised": False}

        def flaky_gather(*aws, **kw):
            if not state["raised"]:
                state["raised"] = True
                raise RuntimeError("gather blew up")
            return real_gather(*aws, **kw)

        async def main():
            async def worker(i):
                await asyncio.sleep(3600) if i == "hang" else await asyncio.sleep(0)
                return i

            monkeypatch.setattr(bw.asyncio, "gather", flaky_gather)
            try:
                with caplog.at_level(logging.ERROR, logger="batch-worker"):
                    return await bw.run_in_batches(["hang", "b"], worker, batch_size=1)
            finally:
                monkeypatch.undo()

        out = run(main())
        # first batch: gather failed -> its item is now COUNTED and RECORDED as an error (it used to vanish:
        # raw=[] -> neither processed nor in errors); second batch ("b") ran normally
        assert out.results == ["b"]
        assert out.processed == 2
        assert out.errors == [{"item": "hang", "error": "RuntimeError: gather blew up"}]
        assert any("batch gather failed" in r.getMessage() for r in caplog.records)

    def _fail_first_gather(self, monkeypatch, exc):
        """Make the FIRST asyncio.gather call (the batch's worker gather) raise `exc`."""
        real_gather = asyncio.gather
        state = {"raised": False}

        def flaky_gather(*aws, **kw):
            if not state["raised"]:
                state["raised"] = True
                raise exc
            return real_gather(*aws, **kw)

        monkeypatch.setattr(bw.asyncio, "gather", flaky_gather)
        return real_gather

    def test_gather_failure_keeps_the_universe_total_whole(self, gc_calls, monkeypatch):
        # 5 items, batch_size 5, gather fails: every item is accounted for exactly once.
        self._fail_first_gather(monkeypatch, RuntimeError("boom"))

        async def worker(i):
            await asyncio.sleep(3600)

        out = run(bw.run_in_batches([1, 2, 3, 4, 5], worker, batch_size=5))
        assert out.processed == 5 and out.results == []
        assert [e["item"] for e in out.errors] == ["1", "2", "3", "4", "5"]
        assert all(e["error"] == "RuntimeError: boom" for e in out.errors)
        assert out.processed == len(out.results) + len(out.errors)

    def test_gather_failure_still_reports_progress_for_the_batch(self, gc_calls, monkeypatch):
        self._fail_first_gather(monkeypatch, RuntimeError("boom"))
        seen = []

        async def on_progress(p):
            seen.append((p.batch_index, p.processed))

        async def worker(i):
            await asyncio.sleep(3600)

        run(bw.run_in_batches([1, 2, 3], worker, batch_size=3, on_progress=on_progress))
        assert seen == [(0, 3)]  # it used to report 0 processed for a batch whose items were dropped

    def test_gather_failure_keeps_workers_that_had_already_finished(self, gc_calls, monkeypatch):
        # "fast" finishes before the failure is handled; "slow" is still running and gets cancelled.
        self._fail_first_gather(monkeypatch, RuntimeError("boom"))
        seen_cancel = []

        async def worker(i):
            if i == "fast":
                return "FAST"
            if i == "bad":
                raise ValueError("worker error")
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                seen_cancel.append(i)
                raise

        async def main():
            # let the tasks start and the quick ones finish before the (injected) gather failure surfaces
            orig_cancel = bw._cancel_tasks

            async def delayed_cancel(tasks, timeout=2.0):
                await asyncio.sleep(0)
                await orig_cancel(tasks, timeout)

            monkeypatch.setattr(bw, "_cancel_tasks", delayed_cancel)
            return await bw.run_in_batches(["fast", "bad", "slow"], worker, batch_size=3)

        out = run(main())
        assert out.results == ["FAST"]
        errs = {e["item"]: e["error"] for e in out.errors}
        assert errs == {"bad": "ValueError: worker error", "slow": "RuntimeError: boom"}
        assert out.processed == 3 and seen_cancel == ["slow"]

    def test_gather_failure_without_error_collection_still_counts_the_items(self, gc_calls, monkeypatch):
        self._fail_first_gather(monkeypatch, RuntimeError("boom"))

        async def worker(i):
            await asyncio.sleep(3600)

        out = run(bw.run_in_batches([1, 2], worker, batch_size=2, collect_errors_from_exceptions=False))
        assert out.processed == 2 and out.errors == [] and out.results == []

    def test_gather_failure_in_one_batch_does_not_touch_the_next(self, gc_calls, monkeypatch):
        self._fail_first_gather(monkeypatch, RuntimeError("boom"))

        async def worker(i):
            if i in (1, 2):
                await asyncio.sleep(3600)
            return i * 10

        out = run(bw.run_in_batches([1, 2, 3, 4], worker, batch_size=2))
        assert out.results == [30, 40]
        assert [e["item"] for e in out.errors] == ["1", "2"]
        assert out.processed == 4

    def test_cache_get_cancellation_propagates(self, gc_calls):
        def cache_get(item):
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            run(bw.run_in_batches([1], echo, cache_get=cache_get))


# ─────────────────────────────── cache ───────────────────────────────

class TestCache:
    def test_hits_skip_the_worker_but_count_as_processed(self, gc_calls):
        cache = {"a": "A-cached", "c": "C-cached"}
        calls = []

        async def worker(i):
            calls.append(i)
            return i.upper()

        out = run(bw.run_in_batches(["a", "b", "c", "d"], worker, batch_size=4, cache_get=cache.get))
        assert calls == ["b", "d"]
        assert out.cache_hits == 2 and out.cache_misses == 2
        assert out.processed == 4
        assert out.results == ["A-cached", "C-cached", "B", "D"]        # cached results are applied first

    def test_successful_results_are_stored(self, gc_calls):
        store = {}
        out = run(bw.run_in_batches(
            ["a", "b"], upper, batch_size=2,
            cache_get=store.get, cache_set=lambda item, r: store.__setitem__(item, r),
        ))
        assert store == {"a": "A", "b": "B"}
        assert out.cache_misses == 2

    def test_second_run_is_all_hits(self, gc_calls):
        store = {}
        calls = []

        async def worker(i):
            calls.append(i)
            return i * 2

        kw = dict(batch_size=3, cache_get=store.get, cache_set=lambda i, r: store.__setitem__(i, r))
        run(bw.run_in_batches([1, 2, 3], worker, **kw))
        out = run(bw.run_in_batches([1, 2, 3], worker, **kw))
        assert calls == [1, 2, 3]                                       # worker only ran the first time
        assert out.cache_hits == 3 and out.cache_misses == 0
        assert out.results == [2, 4, 6]

    def test_cached_falsy_values_count_as_hits(self, gc_calls):
        # only None means "miss": 0 / "" / [] are legitimate cached results
        cache = {1: 0, 2: "", 3: []}
        out = run(bw.run_in_batches([1, 2, 3], echo, cache_get=cache.get))
        assert out.cache_hits == 3
        assert out.results == [0, "", []]

    def test_cache_get_and_set_run_off_the_event_loop_thread(self, gc_calls):
        main_thread = {}
        threads = {"get": set(), "set": set()}

        def cache_get(item):
            threads["get"].add(threading.get_ident())
            return None

        def cache_set(item, r):
            threads["set"].add(threading.get_ident())

        async def main():
            main_thread["id"] = threading.get_ident()
            return await bw.run_in_batches([1, 2, 3], echo, cache_get=cache_get, cache_set=cache_set)

        run(main())
        assert threads["get"] and threads["set"]
        assert main_thread["id"] not in threads["get"] | threads["set"]

    def test_cache_get_exception_is_treated_as_a_miss(self, gc_calls, caplog):
        def cache_get(item):
            if item == "bad":
                raise ConnectionError("db down")
            return None

        with caplog.at_level(logging.DEBUG, logger="batch-worker"):
            out = run(bw.run_in_batches(["ok", "bad"], echo, batch_size=2, cache_get=cache_get))
        assert out.results == ["ok", "bad"]                             # the worker still ran for "bad"
        assert out.cache_misses == 2 and out.cache_hits == 0
        assert any("cache_get failed" in r.getMessage() for r in caplog.records)

    def test_cache_set_exception_is_swallowed_and_result_kept(self, gc_calls, caplog):
        def cache_set(item, r):
            raise OSError("disk full")

        with caplog.at_level(logging.DEBUG, logger="batch-worker"):
            out = run(bw.run_in_batches([1, 2], echo, cache_set=cache_set))
        assert out.results == [1, 2] and out.errors == []
        assert any("cache_set failed" in r.getMessage() for r in caplog.records)

    def test_cache_set_also_applies_to_cache_hits(self, gc_calls):
        # Documents current behaviour: a cache hit is "accepted" like a fresh result, so it is written back.
        writes = []
        run(bw.run_in_batches(
            [1], echo, cache_get=lambda i: "cached", cache_set=lambda i, r: writes.append((i, r)),
        ))
        assert writes == [(1, "cached")]

    def test_cache_set_without_cache_get(self, gc_calls):
        writes = []
        out = run(bw.run_in_batches([1, 2], echo, cache_set=lambda i, r: writes.append((i, r))))
        assert sorted(writes) == [(1, 1), (2, 2)]
        assert out.cache_hits == 0 and out.cache_misses == 2

    def test_cache_key_fn_is_accepted_and_unused(self, gc_calls):
        called = []
        out = run(bw.run_in_batches([1], echo, cache_key_fn=lambda i: called.append(i) or "k"))
        assert out.results == [1] and called == []

    def test_cache_flag_is_logged(self, gc_calls, caplog):
        with caplog.at_level(logging.INFO, logger="batch-worker"):
            run(bw.run_in_batches([1], echo, cache_get=lambda i: None))
        assert any("cache=True" in r.getMessage() for r in caplog.records)

    def test_all_hits_batch_creates_no_tasks(self, gc_calls):
        async def worker(i):
            raise AssertionError("worker must not run")

        out = run(bw.run_in_batches([1, 2], worker, cache_get=lambda i: i * 10))
        assert out.results == [10, 20] and out.errors == []
        assert out.cache_hits == 2

    def test_progress_reports_cache_counters(self, gc_calls):
        rec = Recorder()
        cache = {1: "x"}
        run(bw.run_in_batches([1, 2], echo, batch_size=2, cache_get=cache.get, on_progress=rec))
        (p,) = rec.seen
        assert (p.cache_hits, p.cache_misses) == (1, 1)


# ─────────────────────────────── progress callbacks ───────────────────────────────

class TestProgress:
    def test_on_progress_fires_after_each_batch(self, gc_calls, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 105.0)
        rec = Recorder()
        run(bw.run_in_batches(list(range(5)), echo, batch_size=2, on_progress=rec, start_time=100.0))
        assert [(p.batch_index, p.batch_count, p.processed, p.total) for p in rec.seen] == [
            (0, 3, 2, 5), (1, 3, 4, 5), (2, 3, 5, 5),
        ]
        assert all(p.elapsed_sec == 5.0 and p.cancelled is False for p in rec.seen)

    def test_elapsed_is_rounded_to_one_decimal(self, gc_calls, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 100.26)
        rec = Recorder()
        run(bw.run_in_batches([1], echo, on_progress=rec, start_time=100.0))
        assert rec.seen[0].elapsed_sec == 0.3

    def test_start_time_defaults_to_now(self, gc_calls, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 500.0)
        rec = Recorder()
        run(bw.run_in_batches([1], echo, on_progress=rec))
        assert rec.seen[0].elapsed_sec == 0.0

    def test_start_time_zero_is_respected(self, gc_calls, monkeypatch):
        # `start_time is not None` (not truthiness): 0.0 is a valid explicit start
        monkeypatch.setattr(time, "time", lambda: 7.0)
        rec = Recorder()
        run(bw.run_in_batches([1], echo, on_progress=rec, start_time=0.0))
        assert rec.seen[0].elapsed_sec == 7.0

    def test_on_batch_end_fires_after_on_progress(self, gc_calls):
        order = []

        async def prog(p):
            order.append(("progress", p.batch_index))

        async def end(p):
            order.append(("end", p.batch_index))

        run(bw.run_in_batches([1, 2], echo, batch_size=1, on_progress=prog, on_batch_end=end))
        assert order == [("progress", 0), ("end", 0), ("progress", 1), ("end", 1)]

    def test_progress_callback_failure_is_swallowed(self, gc_calls, caplog):
        rec = Recorder(raises=RuntimeError("ws closed"))
        with caplog.at_level(logging.DEBUG, logger="batch-worker"):
            out = run(bw.run_in_batches([1, 2], echo, batch_size=1, on_progress=rec))
        assert out.results == [1, 2] and len(rec.seen) == 2            # keeps going after the failure
        assert any("on_progress:" in r.getMessage() for r in caplog.records)

    def test_batch_end_callback_failure_is_swallowed(self, gc_calls, caplog):
        rec = Recorder(raises=RuntimeError("hook broke"))
        with caplog.at_level(logging.DEBUG, logger="batch-worker"):
            out = run(bw.run_in_batches([1, 2], echo, batch_size=1, on_batch_end=rec))
        assert out.results == [1, 2] and len(rec.seen) == 2
        assert any("on_batch_end:" in r.getMessage() for r in caplog.records)

    def test_progress_counts_errors_and_cancelled_workers_as_processed(self, gc_calls):
        async def worker(i):
            if i == 1:
                raise RuntimeError("x")
            return i

        rec = Recorder()
        run(bw.run_in_batches([0, 1, 2], worker, batch_size=3, on_progress=rec))
        assert rec.seen[0].processed == 3


# ─────────────────────────────── cancellation ───────────────────────────────

class TestCancellation:
    def test_cancel_before_start_processes_nothing(self, gc_calls):
        rec = Recorder()
        out = run(bw.run_in_batches([1, 2, 3], echo, should_cancel=lambda: True, on_progress=rec))
        assert out.cancelled is True
        assert out.processed == 0 and out.results == []
        assert rec.seen == []                                           # no batch ran -> no progress event
        assert len(gc_calls) == 1                                       # final gc only

    def test_cancel_between_batches_stops_cleanly(self, gc_calls):
        # should_cancel is polled twice per batch (top of loop, then after the batch), so the
        # first True after two full polls means "cancel after batch 2".
        polls = {"n": 0}

        def should_cancel():
            polls["n"] += 1
            return polls["n"] > 3

        rec = Recorder()
        out = run(bw.run_in_batches(list(range(10)), echo, batch_size=2,
                                    should_cancel=should_cancel, on_progress=rec))
        assert out.cancelled is True
        assert out.processed == 4 and out.results == [0, 1, 2, 3]
        assert [p.cancelled for p in rec.seen] == [False, True]

    def test_cancel_after_batch_still_reports_that_batch(self, gc_calls):
        answers = iter([False, True])                                   # top-of-loop False, post-batch True
        rec = Recorder()
        out = run(bw.run_in_batches([1, 2, 3, 4], echo, batch_size=2,
                                    should_cancel=lambda: next(answers, True), on_progress=rec))
        assert out.cancelled is True and out.processed == 2
        assert len(rec.seen) == 1
        assert rec.seen[0].cancelled is True and rec.seen[0].processed == 2

    def test_cancel_still_fires_on_batch_end_and_gc(self, gc_calls):
        answers = iter([False, True])
        end = Recorder()
        run(bw.run_in_batches([1, 2, 3], echo, batch_size=1,
                              should_cancel=lambda: next(answers, True), on_batch_end=end))
        assert len(end.seen) == 1 and end.seen[0].cancelled is True
        assert len(gc_calls) == 2                                       # once for the batch, once at the end

    def test_cancel_logs_summary(self, gc_calls, caplog):
        answers = iter([False, True])
        with caplog.at_level(logging.INFO, logger="batch-worker"):
            run(bw.run_in_batches([1, 2, 3], echo, batch_size=1, should_cancel=lambda: next(answers, True)))
        msgs = [r.getMessage() for r in caplog.records]
        assert any(m.startswith("batch_worker cancelled after 1/3") for m in msgs)
        assert any("cancelled=True" in m for m in msgs)

    def test_never_cancelling_predicate(self, gc_calls):
        out = run(bw.run_in_batches([1, 2, 3], echo, batch_size=1, should_cancel=lambda: False))
        assert out.cancelled is False and out.processed == 3


# ─────────────────────────────── gc ───────────────────────────────

class TestGc:
    def test_collects_once_per_batch_plus_final(self, gc_calls):
        run(bw.run_in_batches(list(range(5)), echo, batch_size=2))
        assert len(gc_calls) == 3 + 1

    def test_gc_can_be_disabled(self, gc_calls):
        run(bw.run_in_batches(list(range(5)), echo, batch_size=2, gc_each_batch=False))
        assert gc_calls == []

    def test_real_gc_is_used_by_default(self):
        # no monkeypatching: just prove the real gc.collect path runs without error
        out = run(bw.run_in_batches([1, 2, 3], echo, batch_size=2))
        assert out.results == [1, 2, 3]
