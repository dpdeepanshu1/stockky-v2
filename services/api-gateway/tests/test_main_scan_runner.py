"""tests/test_main_scan_runner.py — coverage for api-gateway/main.py, slice 4 (lines 3315-3855)

Pass 62. `run_scan_parallel` (the full-universe market scan every "Run Market Scan" click and the
legacy /scan wrapper go through) and `_get_nifty50_data` (the movers fetch behind
/market/top-gainers, /top-losers and /most-active):

* start-up: progress flag, pause / cancel-flag reset, first status write, universe prioritising;
* the single bulk Neon feed read (hit counting, every failure shape) and the cold-feed pre-wake;
* seeding the batch-result cache from the last scan, and the "Power Off" abort;
* cancel detection (task flag, ``__ALL__``, pause, Redis key), per-batch progress + WS push,
  the mid-scan warm rule, batch sizing, and the batch-result cache on / off;
* everything after the batches: sort, top picks, the three horizon boards and their fallbacks,
  verdict / mood / stats, the done payload, LAST_FULL_SCAN cache, metrics and the notification;
* `_get_nifty50_data`: cache hit, pre-open / closed last-known fallback, the locked cold fetch
  (symbol mix, row maths, failure handling, cache writes, post-lock re-check, concurrent callers).

`run_in_batches` is the REAL one (already covered by test_batch_worker.py) so the scan is exercised
end to end; everything the scan talks to is faked: Redis/KV helpers (one in-memory dict), the
per-symbol worker, the Data Feed bulk read, the wake / warm helpers, the WebSocket push, the
notification sender, metrics, yfinance. `asyncio.sleep` is faked so nothing waits. One unreachable
branch (an ERROR row inside `results`, which the classifier always diverts to `errors`) is reached
by stubbing `run_in_batches` and the test says so. Findings are pinned as current behaviour and
marked ``NOT FIXED``.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_scan_runner.py -v
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import datetime

import pandas as pd
import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)

import data_feed as _df
from batch_worker import BatchResult

_REAL_SLEEP = asyncio.sleep
_REAL_WAIT_FOR = asyncio.wait_for


# ── fakes ────────────────────────────────────────────────────────────────────

class RecLogger:
    def __init__(self):
        self.msgs = {"debug": [], "info": [], "warning": [], "error": []}

    def _rec(self, level, msg, args):
        try:
            self.msgs[level].append(msg % args if args else msg)
        except Exception:
            self.msgs[level].append(str(msg))

    def debug(self, msg, *a, **k):
        self._rec("debug", msg, a)

    def info(self, msg, *a, **k):
        self._rec("info", msg, a)

    def warning(self, msg, *a, **k):
        self._rec("warning", msg, a)

    def error(self, msg, *a, **k):
        self._rec("error", msg, a)

    def any(self, level, fragment):
        return any(fragment in m for m in self.msgs[level])


class FakeMetrics:
    def __init__(self):
        self.incs, self.gauges, self.raise_on = [], [], False

    def inc(self, name, *a, **k):
        if self.raise_on:
            raise RuntimeError("metrics down")
        self.incs.append(name)

    def set_gauge(self, name, value, *a, **k):
        self.gauges.append((name, value))


def row(sym, decision="HOLD", score=50, close=100.0, fund=None, **extra):
    r = {"symbol": sym, "decision": decision, "combined_score": score, "close": close}
    if fund is not None:
        r["fundamental_score"] = fund
    r.update(extra)
    return r


def syms(n, prefix="S"):
    return [f"{prefix}{i:02d}" for i in range(n)]


class Env:
    """Scripts every collaborator of `run_scan_parallel` in one place."""

    TASK = "T1"

    def __init__(self, mp):
        self.mp = mp
        self.store = {}
        self.sets = []                 # (key, value, ttl) in write order
        self.get_raises = set()        # keys whose _redis_get raises
        self.client = object()
        self.worker_calls = []         # kwargs dicts, one per worker call
        self.rows = {}                 # sym -> dict | Exception | callable(sym)
        self.on_call = None            # hook(sym) run inside every worker call
        self.feeds = {}
        self.feeds_fn = None
        self.wake_calls, self.wake_raises = [], None
        self.warm_calls, self.warm_raises = [], None
        self.ws_calls, self.ws_raises = [], False
        self.notify_calls, self.notify_raises = [], None
        self.outcomes = []
        self.watchlist = []
        self.prioritized = lambda u: list(u)
        self.sleeps = []
        self.wait_for_timeouts = []
        self.metrics = FakeMetrics()
        self.log = RecLogger()

    # -- collaborators ------------------------------------------------------
    def install(self):
        mp = self.mp

        def fake_get(key):
            if key in self.get_raises:
                raise RuntimeError("redis get boom")
            return self.store.get(key)

        def fake_set(key, value, ttl=None):
            self.sets.append((key, value, ttl))
            self.store[key] = value

        async def fake_analyze(sym, client, sem, lite=False, feed_row=None,
                               prefetched_feeds=None, skip_gemini=False, **kw):
            self.worker_calls.append({
                "sym": sym, "client": client, "sem": sem, "lite": lite, "feed_row": feed_row,
                "prefetched_feeds": prefetched_feeds, "skip_gemini": skip_gemini,
                "in_progress": gw._SCAN_IN_PROGRESS,
            })
            if self.on_call:
                self.on_call(sym)
            await _REAL_SLEEP(0)
            spec = self.rows.get(sym)
            if isinstance(spec, BaseException):
                raise spec
            if callable(spec):
                return spec(sym)
            if spec is not None:
                return spec
            return row(sym)

        async def fake_wake(client=None):
            self.wake_calls.append(client)
            if self.wake_raises:
                raise self.wake_raises
            return {"decision": {"ok": True}, "market-data": {"ok": False}}

        async def fake_warm(client=None):
            self.warm_calls.append(client)
            if self.warm_raises:
                raise self.warm_raises

        async def fake_ws(task_id, data):
            self.ws_calls.append((task_id, data))
            if self.ws_raises:
                raise RuntimeError("ws down")

        async def fake_sleep(delay=0, *a, **k):
            self.sleeps.append(delay)
            await _REAL_SLEEP(0)

        async def fake_wait_for(aw, timeout=None):
            self.wait_for_timeouts.append(timeout)
            return await _REAL_WAIT_FOR(aw, timeout)

        def fake_notify(recs, verdict, scanned, universe_size):
            self.notify_calls.append((recs, verdict, scanned, universe_size))
            if self.notify_raises:
                raise self.notify_raises

        def fake_feeds(bases):
            self.feeds_asked = list(bases)
            if self.feeds_fn:
                return self.feeds_fn(bases)
            return self.feeds

        mp.setattr(gw, "_redis_get", fake_get)
        mp.setattr(gw, "_redis_set", fake_set)
        mp.setattr(gw, "_redis_soft_ttl_refresh", lambda *a, **k: False)
        mp.setattr(gw, "_get_http_client", lambda: self.client)
        mp.setattr(gw, "_prioritize_universe", lambda u: self.prioritized(u))
        mp.setattr(gw, "_analyze_one_symbol_ultra", fake_analyze)
        mp.setattr(gw, "_wake_required_services", fake_wake)
        mp.setattr(gw, "_warm_upstream_services", fake_warm)
        mp.setattr(gw, "_ws_push_scan", fake_ws)
        mp.setattr(gw, "_send_scan_notification", fake_notify)
        mp.setattr(gw, "_record_symbol_outcomes", lambda r: self.outcomes.append(r))
        mp.setattr(gw, "_load_watchlist", lambda: list(self.watchlist))
        mp.setattr(gw, "metrics", self.metrics)
        mp.setattr(gw, "request_data_feed_stop", lambda: None)
        mp.setattr(gw, "clear_data_feed_stop", lambda: None)
        mp.setattr(_df, "get_all_stock_feeds", fake_feeds)
        mp.setattr(gw.asyncio, "sleep", fake_sleep)
        mp.setattr(gw.asyncio, "wait_for", fake_wait_for)
        mp.setattr(gw, "logger", self.log)
        mp.setattr(gw, "_ACTIVITY_PAUSED", False)
        mp.setattr(gw, "_SCAN_IN_PROGRESS", False)
        mp.setattr(gw, "_SCAN_CANCEL_FLAGS", set())
        mp.setattr(gw, "WAKE_BEFORE_SCAN", False)
        mp.setattr(gw, "WAKE_WAIT_SECONDS", 12.0)
        mp.setattr(gw, "BATCH_RESULT_CACHE_ENABLED", True)
        mp.setattr(gw, "SCAN_BATCH_SIZE", 8)
        mp.setattr(gw, "MAX_PARALLEL_WORKERS", 8)
        return self

    # -- helpers ------------------------------------------------------------
    @property
    def task_key(self):
        return gw.SCAN_TASK_PREFIX + self.TASK

    def run(self, universe, lite=False):
        asyncio.run(gw.run_scan_parallel(self.TASK, list(universe), lite))
        return self.store.get(self.task_key)

    def task_writes(self):
        return [v for k, v, _t in self.sets if k == self.task_key]

    def result(self, universe, lite=False):
        return self.run(universe, lite)["result"]

    def worker_syms(self):
        return [c["sym"] for c in self.worker_calls]

    def cache_key(self, sym, lite=False):
        return gw._batch_result_cache_key(sym, lite)

    def precache(self, sym, decision="HOLD", lite=False, **extra):
        self.store[self.cache_key(sym, lite)] = {"symbol": sym, "decision": decision,
                                                 "combined_score": 50, **extra}


@pytest.fixture
def env(monkeypatch):
    return Env(monkeypatch).install()


# ── start-up ─────────────────────────────────────────────────────────────────

class TestStartup:
    def test_progress_flag_is_true_while_scanning_and_false_after(self, env):
        env.run(syms(3))
        assert all(c["in_progress"] is True for c in env.worker_calls)
        assert gw._SCAN_IN_PROGRESS is False
        assert gw.scan_in_progress() is False

    def test_clears_pause_and_the_global_cancel_flag_only(self, env, monkeypatch):
        monkeypatch.setattr(gw, "_ACTIVITY_PAUSED", True)
        gw._SCAN_CANCEL_FLAGS.update({"__ALL__", "some-other-task"})
        env.run(syms(2))
        assert gw._ACTIVITY_PAUSED is False
        assert "__ALL__" not in gw._SCAN_CANCEL_FLAGS
        assert "some-other-task" in gw._SCAN_CANCEL_FLAGS

    def test_a_failing_pause_reset_is_swallowed_and_the_scan_still_runs(self, env, monkeypatch):
        def boom(_paused):
            raise RuntimeError("pause reset failed")
        monkeypatch.setattr(gw, "set_activity_paused", boom)
        payload = env.run(syms(2))
        assert payload["status"] == "done"
        assert len(env.worker_calls) == 2

    def test_first_status_write_is_a_running_record_with_a_one_hour_ttl(self, env):
        env.run(syms(3), lite=True)
        key, first, ttl = env.sets[0]
        assert key == env.task_key and ttl == 3600
        assert first["status"] == "running" and first["total"] == 3 and first["processed"] == 0
        assert first["lite"] is True and first["message"].startswith("Starting")
        assert first["result"] is None and first["error"] is None

    def test_universe_is_prioritised_before_it_is_counted_and_scanned(self, env):
        env.prioritized = lambda u: list(reversed(u))
        env.run(["A", "B", "C"])
        assert env.worker_syms() == ["C", "B", "A"]

    def test_lite_flag_reaches_the_worker_along_with_skip_gemini(self, env):
        env.run(["A"], lite=True)
        call = env.worker_calls[0]
        assert call["lite"] is True and call["skip_gemini"] is True
        assert call["client"] is env.client and isinstance(call["sem"], asyncio.Semaphore)


# ── bulk feed ────────────────────────────────────────────────────────────────

class TestBulkFeed:
    def test_bases_are_upper_case_with_exchange_suffix_and_whitespace_stripped(self, env):
        env.run(["reliance.ns", "TCS.BO", " infy "])
        assert env.feeds_asked == ["RELIANCE", "TCS", "INFY"]

    def test_hit_count_uses_each_of_the_four_field_tests(self, env):
        env.feeds = {
            "A": {"fundamental_score": 0},          # 0 is not None -> hit
            "B": {"metrics": {"pe": 12}},
            "C": {"sector": "IT"},
            "D": {"close": 0},                      # 0 is not None -> hit
            "E": {},                                # falsy row -> no hit
            "F": {"fundamental_score": None, "metrics": {}, "sector": "", "close": None},
        }
        env.run(list("ABCDEF"))
        msgs = [w.get("message") for w in env.task_writes()]
        assert "Neon bulk feed 4/6 — starting batches" in msgs

    def test_feed_rows_are_handed_to_the_matching_worker(self, env):
        env.feeds = {"ABC": {"close": 10.0}}
        env.run(["abc.bo", "XYZ"])
        by_sym = {c["sym"]: c for c in env.worker_calls}
        assert by_sym["abc.bo"]["feed_row"] == {"close": 10.0}
        assert by_sym["XYZ"]["feed_row"] is None
        assert by_sym["XYZ"]["prefetched_feeds"] is env.feeds

    def test_a_none_bulk_read_is_treated_as_an_empty_dict(self, env):
        env.feeds_fn = lambda bases: None
        env.run(["A"])
        assert env.worker_calls[0]["feed_row"] is None
        assert env.worker_calls[0]["prefetched_feeds"] == {}

    def test_a_failing_bulk_read_is_swallowed(self, env):
        def boom(bases):
            raise RuntimeError("neon down")
        env.feeds_fn = boom
        payload = env.run(["A", "B"])
        assert payload["status"] == "done"
        assert env.worker_calls[0]["prefetched_feeds"] == {}

    def test_a_non_dict_bulk_read_is_swallowed_and_replaced_by_an_empty_dict(self, env):
        env.feeds_fn = lambda bases: ["not", "a", "dict"]
        payload = env.run(["A"])
        assert payload["status"] == "done"
        assert env.worker_calls[0]["prefetched_feeds"] == {}


# ── pre-scan wake ────────────────────────────────────────────────────────────

class TestWake:
    def test_off_switch_means_no_wake(self, env):
        env.run(syms(3))
        assert env.wake_calls == []

    def test_cold_feed_wakes_services_under_a_twelve_second_bound_then_waits_capped_at_six(self, env, monkeypatch):
        monkeypatch.setattr(gw, "WAKE_BEFORE_SCAN", True)
        env.run(syms(3))
        assert env.wake_calls == [env.client]
        assert 12.0 in env.wait_for_timeouts
        assert env.sleeps[0] == 6.0

    @pytest.mark.parametrize("configured, expected", [(2, 2.0), (0, 4.0), (30, 6.0)])
    def test_post_wake_sleep_is_min_of_configured_or_four_and_six(self, env, monkeypatch, configured, expected):
        monkeypatch.setattr(gw, "WAKE_BEFORE_SCAN", True)
        monkeypatch.setattr(gw, "WAKE_WAIT_SECONDS", configured)
        env.run(["A"])
        assert env.sleeps[0] == expected

    def test_warm_feed_skips_the_wake(self, env, monkeypatch):
        monkeypatch.setattr(gw, "WAKE_BEFORE_SCAN", True)
        env.feeds = {"A": {"close": 1}, "B": {"close": 1}}
        env.run(["A", "B", "C", "D"])           # ratio exactly 0.5 -> not < 0.5 -> skip
        assert env.wake_calls == []

    def test_just_under_half_still_wakes(self, env, monkeypatch):
        monkeypatch.setattr(gw, "WAKE_BEFORE_SCAN", True)
        env.feeds = {"A": {"close": 1}}
        env.run(["A", "B", "C"])
        assert len(env.wake_calls) == 1

    @pytest.mark.parametrize("exc", [RuntimeError("wake failed"), asyncio.TimeoutError()])
    def test_wake_failure_or_timeout_never_stops_the_scan(self, env, monkeypatch, exc):
        monkeypatch.setattr(gw, "WAKE_BEFORE_SCAN", True)
        env.wake_raises = exc
        payload = env.run(syms(2))
        assert payload["status"] == "done"
        assert env.log.any("warning", "Pre-scan wake failed/timeout")
        assert env.sleeps == []                 # the post-wake sleep is skipped

    def test_empty_universe_has_no_division_by_zero_and_wakes_if_enabled(self, env, monkeypatch):
        monkeypatch.setattr(gw, "WAKE_BEFORE_SCAN", True)
        payload = env.run([])
        assert payload["status"] == "done" and payload["total"] == 0
        assert len(env.wake_calls) == 1


# ── seeding from the last scan ───────────────────────────────────────────────

class TestSeedFromLastScan:
    def _last(self, rows, **result_extra):
        return {"result": {"all_results": rows, **result_extra}}

    def test_seeds_rows_and_they_become_cache_hits_this_scan(self, env):
        env.store[gw.LAST_FULL_SCAN_KEY] = self._last([
            {"symbol": "aaa.ns", "decision": "BUY NOW", "combined_score": 80, "_tmp": 1},
            {"symbol": "BBB.BO", "decision": "HOLD", "combined_score": 40},
        ])
        result = env.result(["AAA", "BBB", "CCC"])
        assert env.worker_syms() == ["CCC"]     # AAA + BBB served from the seeded cache
        # a seeded row keeps the symbol text it was saved with ("aaa.ns"), only the cache KEY is normalised
        by = {r["symbol"].upper().replace(".NS", "").replace(".BO", ""): r for r in result["all_results"]}
        assert by["AAA"]["_from_batch_cache"] is True and by["AAA"]["decision"] == "BUY NOW"
        assert by["AAA"]["symbol"] == "aaa.ns"
        assert "_tmp" not in env.store[env.cache_key("AAA")]      # underscore keys stripped
        assert env.log.any("info", "Seeded batch_result cache with 2 symbols")

    def test_skips_non_dicts_blank_symbols_errors_and_already_cached(self, env):
        env.precache("DDD", decision="HOLD")
        env.store[gw.LAST_FULL_SCAN_KEY] = self._last([
            "junk", None, {"decision": "BUY NOW"}, {"symbol": "", "decision": "BUY NOW"},
            {"symbol": "EEE", "decision": "ERROR"},
            {"symbol": "DDD", "decision": "BUY NOW", "combined_score": 99},
            {"symbol": "FFF", "decision": "BUY NOW"},
        ])
        env.run(["DDD", "EEE", "FFF"])
        assert env.worker_syms() == ["EEE"]                      # EEE was never seeded
        assert env.store[env.cache_key("DDD")]["decision"] == "HOLD"   # kept, not overwritten
        assert env.log.any("info", "Seeded batch_result cache with 1 symbols")

    @pytest.mark.parametrize("last", [None, "text", [], {"result": None}, {"result": {"all_results": None}}, {}])
    def test_odd_last_scan_shapes_seed_nothing_and_do_not_crash(self, env, last):
        if last is not None:
            env.store[gw.LAST_FULL_SCAN_KEY] = last
        payload = env.run(["A"])
        assert payload["status"] == "done"
        assert not env.log.any("info", "Seeded batch_result cache")

    def test_a_failing_read_of_the_last_scan_is_swallowed(self, env):
        env.get_raises.add(gw.LAST_FULL_SCAN_KEY)
        payload = env.run(["A"])
        assert payload["status"] == "done"
        assert env.log.any("debug", "seed batch cache")

    def test_finding_lite_rows_from_the_last_scan_are_seeded_into_the_full_cache(self, env):
        """NOT FIXED (low-medium): seeding uses the CURRENT scan's `lite` for the cache key and never
        looks at the last scan's own ``lite`` flag, so a lite scan's score-ladder rows (no
        fundamentals / news) are served by the next FULL scan as if they were full results."""
        env.store[gw.LAST_FULL_SCAN_KEY] = self._last(
            [{"symbol": "AAA", "decision": "BUY NOW", "combined_score": 61, "lite": True}], lite=True)
        result = env.result(["AAA"], lite=False)
        assert env.worker_calls == []                            # the full scan never analysed AAA
        assert result["all_results"][0]["_from_batch_cache"] is True
        assert result["all_results"][0]["lite"] is True

    def test_finding_last_scan_age_is_ignored_and_the_row_gets_a_fresh_ttl(self, env):
        """NOT FIXED (low): a row from a scan that finished a day ago is re-cached with a new TTL
        and served as fresh (last-scan record lives 24h)."""
        env.store[gw.LAST_FULL_SCAN_KEY] = self._last(
            [{"symbol": "AAA", "decision": "BUY NOW", "combined_score": 61}],
            scanned_at="2026-09-28T09:00:00+05:30")
        env.run(["AAA"])
        assert env.worker_calls == []
        assert any(k == env.cache_key("AAA") and t == gw._decide_cache_ttl() for k, _v, t in env.sets)

    def test_finding_seeded_count_is_logged_even_when_the_batch_cache_is_disabled(self, env, monkeypatch):
        """NOT FIXED (very low): with BATCH_RESULT_CACHE off the set is a no-op but `seeded` still
        counts, so the log claims rows were seeded that were never stored."""
        monkeypatch.setattr(gw, "BATCH_RESULT_CACHE_ENABLED", False)
        env.store[gw.LAST_FULL_SCAN_KEY] = self._last([{"symbol": "AAA", "decision": "BUY NOW"}])
        env.run(["AAA"])
        assert env.log.any("info", "Seeded batch_result cache with 1 symbols")
        assert env.cache_key("AAA") not in env.store
        assert env.worker_syms() == ["AAA"]


# ── power-off abort ──────────────────────────────────────────────────────────

class TestPausedAbort:
    def test_scan_aborts_cleanly_when_activity_is_still_paused(self, env, monkeypatch):
        monkeypatch.setattr(gw, "set_activity_paused", lambda p: None)   # the reset does not stick
        monkeypatch.setattr(gw, "_ACTIVITY_PAUSED", True)
        assert asyncio.run(gw.run_scan_parallel(env.TASK, ["A", "B"], False)) is None
        rec = env.store[env.task_key]
        assert rec["status"] == "cancelled" and rec["error"] == "activity_paused"
        assert rec["cancelled"] is True and rec["partial"] is True and rec["processed"] == 0
        assert rec["total"] == 2 and "Powered Off" in rec["message"]
        assert gw._SCAN_IN_PROGRESS is False
        assert env.worker_calls == [] and env.notify_calls == [] and env.ws_calls == []
        assert gw.LAST_FULL_SCAN_KEY not in env.store
        assert [t for k, _v, t in env.sets if k == env.task_key][-1] == 3600


# ── cancel detection ─────────────────────────────────────────────────────────

class TestCancel:
    def _cancel_by(self, env, monkeypatch, how):
        def hook(sym):
            if sym != "S00":
                return
            if how == "task":
                gw._SCAN_CANCEL_FLAGS.add(env.TASK)
            elif how == "all":
                gw._SCAN_CANCEL_FLAGS.add("__ALL__")
            elif how == "pause":
                monkeypatch.setattr(gw, "_ACTIVITY_PAUSED", True)
            elif how == "redis":
                env.store[env.task_key + ":cancel"] = True
        env.on_call = hook

    @pytest.mark.parametrize("how", ["task", "all", "pause", "redis"])
    def test_each_cancel_signal_stops_the_scan_after_the_running_batch(self, env, monkeypatch, how):
        self._cancel_by(env, monkeypatch, how)
        payload = env.run(syms(24))
        assert payload["status"] == "done"                        # UI can always load the partial
        assert payload["cancelled"] is True and payload["partial"] is True
        assert payload["processed"] == 8 and payload["total"] == 24
        res = payload["result"]
        assert res["cancelled"] is True and res["partial"] is True and res["stopped_early"] is True
        assert res["scanned"] == 8
        assert res["verdict"].startswith("Scan stopped early (8/24 stocks checked) — ")
        assert env.log.any("info", "cancelled after 8/24")

    def test_partial_scan_is_cached_but_not_notified(self, env, monkeypatch):
        self._cancel_by(env, monkeypatch, "task")
        env.run(syms(24))
        last = env.store[gw.LAST_FULL_SCAN_KEY]
        assert last["partial"] is True and last["cancelled"] is True
        assert last["processed"] == 8 and last["total"] == 24
        assert env.notify_calls == []

    def test_cancel_before_the_first_batch_gives_an_empty_done_record_and_no_cache_write(self, env):
        gw._SCAN_CANCEL_FLAGS.add(env.TASK)
        payload = env.run(syms(5))
        assert payload["cancelled"] is True and payload["processed"] == 0
        assert payload["result"]["all_results"] == []
        assert payload["result"]["verdict"].startswith("Scan stopped early (0/5 stocks checked) — DO NOT BUY")
        assert env.worker_calls == []
        assert gw.LAST_FULL_SCAN_KEY not in env.store and env.notify_calls == []

    def test_pre_set_redis_cancel_key_also_stops_before_batch_one(self, env):
        env.store[env.task_key + ":cancel"] = "1"
        assert env.run(syms(5))["processed"] == 0

    def test_cancelled_scan_skips_the_mid_scan_warm(self, env, monkeypatch):
        monkeypatch.setattr(gw, "SCAN_BATCH_SIZE", 4)
        env.on_call = lambda s: gw._SCAN_CANCEL_FLAGS.add(env.TASK) if s == "S16" else None
        payload = env.run(syms(40))
        assert payload["processed"] == 20                          # 20 % 20 == 0 but cancelled
        assert env.warm_calls == []


# ── progress, batch sizing, warm rule ────────────────────────────────────────

class TestProgress:
    def test_every_batch_writes_progress_and_pushes_it_over_the_websocket(self, env):
        env.run(syms(20))
        running = [w for w in env.task_writes() if w.get("batch")]
        assert [w["batch"] for w in running] == [1, 2, 3]
        assert all(w["batches"] == 3 and w["status"] == "running" for w in running)
        assert [w["processed"] for w in running] == [8, 16, 20]
        assert running[0]["cache_hits"] == 0 and running[0]["cache_misses"] == 8
        pushed = [d for t, d in env.ws_calls if d["status"] == "running"]
        assert len(pushed) == 3 and env.ws_calls[-1][1]["status"] == "done"
        assert all(t == env.TASK for t, _d in env.ws_calls)

    def test_a_failing_websocket_push_never_breaks_progress_or_the_final_push(self, env):
        env.ws_raises = True
        payload = env.run(syms(10))
        assert payload["status"] == "done"
        assert len(env.ws_calls) == 3           # 2 progress pushes + the done push, all attempted

    @pytest.mark.parametrize("cfg_batch, workers, n, expected_batches", [
        (8, 8, 20, 3),       # min(8, 16) = 8
        (1, 8, 20, 5),       # floor of 4
        (100, 8, 32, 2),     # capped at 2 x workers = 16
        (100, 2, 20, 4),     # default_batch_size(2, minimum=6) = 6
        (5, 8, 20, 4),
    ])
    def test_batch_size_is_config_bounded_by_four_and_the_worker_pool(self, env, monkeypatch, cfg_batch,
                                                                       workers, n, expected_batches):
        monkeypatch.setattr(gw, "SCAN_BATCH_SIZE", cfg_batch)
        monkeypatch.setattr(gw, "MAX_PARALLEL_WORKERS", workers)
        env.run(syms(n))
        assert [w for w in env.task_writes() if w.get("batches")][-1]["batches"] == expected_batches


class TestMidScanWarm:
    def test_warms_upstream_every_time_processed_lands_on_a_multiple_of_twenty(self, env, monkeypatch):
        monkeypatch.setattr(gw, "SCAN_BATCH_SIZE", 4)
        env.run(syms(40))
        assert env.warm_calls == [env.client, env.client]           # at 20 and 40

    def test_no_warm_when_the_universe_is_too_small(self, env, monkeypatch):
        monkeypatch.setattr(gw, "SCAN_BATCH_SIZE", 4)
        env.run(syms(12))
        assert env.warm_calls == []

    def test_a_failing_warm_is_swallowed(self, env, monkeypatch):
        monkeypatch.setattr(gw, "SCAN_BATCH_SIZE", 4)
        env.warm_raises = RuntimeError("warm failed")
        assert env.run(syms(20))["status"] == "done"
        assert len(env.warm_calls) == 1

    def test_mostly_cached_scan_skips_the_warm_at_the_085_boundary(self, env, monkeypatch):
        monkeypatch.setattr(gw, "SCAN_BATCH_SIZE", 4)
        for s in syms(20)[:17]:                                     # 17 / 20 = 0.85 -> skip
            env.precache(s)
        env.run(syms(20))
        assert env.warm_calls == []

    def test_just_under_the_boundary_still_warms(self, env, monkeypatch):
        monkeypatch.setattr(gw, "SCAN_BATCH_SIZE", 4)
        for s in syms(20)[:16]:                                     # 16 / 20 = 0.80 -> warm
            env.precache(s)
        env.run(syms(20))
        assert len(env.warm_calls) == 1

    def test_finding_default_batch_of_eight_only_lines_up_with_twenty_every_forty_symbols(self, env):
        """NOT FIXED (low): the rule is `processed % 20 == 0`, but processed only takes values that
        are multiples of the batch size. With the default 8, the warm fires at 40 / 80 / 120... and
        never at 20, 60, 100 — so a 39-symbol scan never warms at all."""
        env.run(syms(39))
        assert env.warm_calls == []
        env.store.clear()                       # else the 39 cached rows make run two "mostly cached"
        env.run(syms(40))
        assert len(env.warm_calls) == 1


# ── batch-result cache ───────────────────────────────────────────────────────

class TestBatchCache:
    def test_scored_rows_are_cached_and_a_second_scan_does_no_upstream_work(self, env):
        env.rows["S01"] = row("S01", decision="ERROR", error="boom")
        env.run(syms(3))
        assert env.cache_key("S00") in env.store and env.cache_key("S01") not in env.store
        env.worker_calls.clear()
        result = env.result(syms(3))
        assert env.worker_syms() == ["S01"]                          # only the ERROR one is retried
        assert {r["symbol"] for r in result["all_results"] if r.get("_from_batch_cache")} == {"S00", "S02"}
        assert env.log.any("info", "Scan batch cache hits=2 misses=1")

    def test_disabled_cache_means_every_symbol_is_a_miss_and_nothing_is_stored(self, env, monkeypatch):
        monkeypatch.setattr(gw, "BATCH_RESULT_CACHE_ENABLED", False)
        env.run(syms(3))
        env.run(syms(3))
        assert len(env.worker_calls) == 6
        assert not any(k.startswith(gw.BATCH_RESULT_CACHE_PREFIX) for k in env.store)
        # the stats line is still logged (hits=0, every symbol a miss) — harmless, pinned as is
        assert env.log.any("info", "Scan batch cache hits=0 misses=3 (0% hit rate)")

    def test_lite_and_full_scans_use_separate_cache_keys(self, env):
        env.run(syms(2), lite=True)
        assert env.cache_key("S00", lite=True) in env.store and env.cache_key("S00", lite=False) not in env.store
        env.worker_calls.clear()
        # drop the last-scan record: it would otherwise seed the lite rows into the FULL cache (see the
        # "lite rows ... seeded" finding above), which is a different path from the key separation tested here
        env.store.pop(gw.LAST_FULL_SCAN_KEY)
        env.run(syms(2), lite=False)
        assert len(env.worker_calls) == 2
        assert all(c["lite"] is False for c in env.worker_calls)

    def test_empty_universe_logs_no_cache_stats(self, env):
        env.run([])
        assert not env.log.any("info", "Scan batch cache hits")


# ── errors ───────────────────────────────────────────────────────────────────

class TestErrors:
    def test_error_rows_land_in_errors_not_results(self, env):
        env.rows["S01"] = row("S01", decision="ERROR", error="upstream 500")
        env.rows["S02"] = row("S02", decision="ERROR")
        result = env.result(syms(4))
        assert result["scanned"] == 2
        assert result["errors"] == [{"symbol": "S01", "error": "upstream 500"},
                                    {"symbol": "S02", "error": "Unknown error"}]

    def test_a_non_dict_worker_result_is_recorded_as_an_invalid_result(self, env):
        env.rows["S00"] = lambda s: ["not", "a", "dict"]
        result = env.result(syms(2))
        assert result["errors"] == [{"symbol": "?", "error": "invalid result"}]
        assert result["scanned"] == 1

    def test_a_worker_that_raises_is_counted_and_reported_by_the_batch_runner(self, env):
        env.rows["S01"] = ValueError("bad row")
        payload = env.run(syms(3))
        assert payload["processed"] == 3
        assert payload["result"]["errors"] == [{"item": "S01", "error": "ValueError: bad row"}]
        assert payload["result"]["scanned"] == 2


# ── ranking, boards, verdict ─────────────────────────────────────────────────

def buy(sym, score, close=100.0, fund=None, **extra):
    return row(sym, "BUY NOW", score, close, fund, **extra)


class TestResultsAndBoards:
    def test_results_are_sorted_by_combined_score_and_a_missing_score_counts_as_zero(self, env):
        env.rows.update({"A": row("A", score=10), "B": row("B", score=90),
                         "C": {"symbol": "C", "decision": "HOLD"}, "D": row("D", score=50)})
        result = env.result(list("ABCD"))
        assert [r["symbol"] for r in result["all_results"]] == ["B", "D", "A", "C"]
        assert env.outcomes[0] is result["all_results"]           # the sorted list feeds pruning

    def test_top_picks_drive_the_verdict_count_and_are_capped_at_five(self, env):
        for i in range(6):
            env.rows[f"S{i:02d}"] = buy(f"S{i:02d}", 70 + i)
        result = env.result(syms(6))
        assert result["verdict"] == "5 strong opportunity(ies) found"
        assert result["market_stats"]["buy_signals"] == 6
        assert result["watchlist_candidates"] == []

    def test_no_actionable_rows_gives_the_cautious_verdict_and_top_three_as_watchlist(self, env):
        for i in range(5):
            env.rows[f"S{i:02d}"] = row(f"S{i:02d}", "HOLD", 40 + i)
        result = env.result(syms(5))
        assert result["verdict"] == "DO NOT BUY ANY STOCK TODAY — market conditions cautious"
        assert [r["symbol"] for r in result["watchlist_candidates"]] == ["S04", "S03", "S02"]

    def test_final_payload_fields(self, env):
        env.watchlist = ["W1", "W2"]
        env.rows["S00"] = buy("S00", 80)
        payload = env.run(syms(3), lite=True)
        res = payload["result"]
        assert res["universe_size"] == 3 and res["watchlist_size"] == 2 and res["lite"] is True
        assert res["scanned_at"].endswith("+05:30")
        assert res["elapsed_seconds"] == payload["elapsed"] >= 0
        assert res["cancelled"] is False and "partial" not in res
        assert payload["status"] == "done" and payload["cancelled"] is False
        assert payload["partial"] is False and payload["error"] is None
        assert payload["processed"] == payload["total"] == 3
        assert [t for k, _v, t in env.sets if k == env.task_key][-1] == 3600
        assert env.ws_calls[-1][1] is env.store[env.task_key]

    def test_finding_value_adjusted_ranking_never_reaches_the_recommendations(self, env):
        """NOT FIXED (medium): `_select_top_picks` (value-adjusted, BUY-only) only feeds the verdict
        count, the fallback and `watchlist_candidates`. `recommendations` — what the UI shows and
        what the notification sends — is the horizon-short board, ranked by RAW score. The cheap
        fundamentally-sound name never gets its bonus in the list people actually see."""
        env.rows["AAA"] = buy("AAA", 66, close=1900, fund=80)       # adjusted 66.4
        env.rows["BBB"] = buy("BBB", 60, close=200, fund=80)        # adjusted 67.2
        result = env.result(["AAA", "BBB"])
        assert [r["symbol"] for r in gw._select_top_picks(result["all_results"])] == ["BBB", "AAA"]
        assert [r["symbol"] for r in result["recommendations"]] == ["AAA", "BBB"]
        assert result["recommendations"] == result["recommendations_short"]
        assert env.notify_calls[0][0] == result["recommendations"]

    def test_finding_high_score_do_not_buy_rows_are_promoted_while_the_verdict_says_do_not_buy(self, env):
        """NOT FIXED (low-medium): with zero BUY signals the verdict reads "DO NOT BUY ANY STOCK
        TODAY", yet the recommendations hold DO NOT BUY rows relabelled PREPARE TO BUY, and the
        notification goes out as "Top N Picks" with that same contradictory verdict."""
        env.rows["A"] = row("A", "DO NOT BUY", 60)
        env.rows["B"] = row("B", "DO NOT BUY", 55)
        result = env.result(["A", "B"])
        assert result["verdict"].startswith("DO NOT BUY ANY STOCK TODAY")
        assert result["market_stats"]["buy_signals"] == 0 and result["market_mood"] == "Cautious"
        recs = result["recommendations_short"]
        assert [r["decision"] for r in recs] == ["PREPARE TO BUY", "PREPARE TO BUY"]
        assert all(r["promoted_from_score"] is True for r in recs)
        assert env.notify_calls[0][0] == recs and env.notify_calls[0][1] == result["verdict"]

    def test_final_verdict_block_counts_and_names_the_best_short_pick(self, env):
        env.rows["A"] = buy("A", 80)
        env.rows["B"] = buy("B", 70)
        fv = env.result(["A", "B"])["final_verdict"]
        assert fv["preferred_horizon"] == "short" and fv["best_short"] == "A"
        assert fv["short_count"] == fv["mid_count"] == fv["long_count"] == 2
        assert fv["headline"] == "Short-term focus: 2 pick(s). Mid: 2, Long: 2."

    def test_empty_scan_has_no_best_short_pick(self, env):
        res = env.result([])
        assert res["final_verdict"]["best_short"] is None
        assert res["recommendations"] == [] and res["scanned"] == 0
        assert res["verdict"].startswith("DO NOT BUY")


class TestHorizonBoards:
    def test_boards_take_the_horizon_block_score_and_decision_and_rank_by_it(self, env):
        env.rows["A"] = buy("A", 50, horizons={"short": {"score": 60, "decision": "BUY NOW"},
                                                "mid": {"score": 90, "decision": "BUY NOW"}})
        env.rows["B"] = buy("B", 50, horizons={"short": {"score": 80, "decision": "PREPARE TO BUY"}})
        res = env.result(["A", "B"])
        assert [r["symbol"] for r in res["recommendations_short"]] == ["B", "A"]
        assert [r["_hz_score"] for r in res["recommendations_short"]] == [80, 60]
        assert res["recommendations_mid"][0]["symbol"] == "A" and res["recommendations_mid"][0]["_hz_score"] == 90
        assert all(r["horizon_focus"] == "short" for r in res["recommendations_short"])
        assert "_hz_score" not in res["all_results"][0]          # copies, not the scan rows

    def test_a_horizon_decision_overrides_the_row_decision_for_that_board(self, env):
        env.rows["A"] = buy("A", 30, horizons={"mid": {"score": 10, "decision": "DO NOT BUY"}})
        res = env.result(["A"])
        assert "horizon_focus" not in res["recommendations_mid"][0]      # fallback row, not a pick
        assert res["recommendations_short"][0]["horizon_focus"] == "short"

    def test_missing_horizon_scores_fall_back_to_the_combined_score_with_a_mid_and_long_discount(self, env):
        env.rows["A"] = row("A", "DO NOT BUY", 60, fund=70)
        res = env.result(["A"])
        assert res["recommendations_short"][0]["_hz_score"] == pytest.approx(60)
        assert res["recommendations_mid"][0]["_hz_score"] == pytest.approx(57.0)
        assert res["recommendations_long"][0]["_hz_score"] == pytest.approx(70 * 0.9 + 60 * 0.1)

    def test_long_board_without_a_fundamental_score_uses_the_combined_score(self, env):
        env.rows["A"] = row("A", "DO NOT BUY", 60)
        assert env.result(["A"])["recommendations_long"][0]["_hz_score"] == pytest.approx(60)

    @pytest.mark.parametrize("board, key, below, at", [
        ("short", "recommendations_short", 53.9, 54),
        ("mid", "recommendations_mid", 55.9, 56),
        ("long", "recommendations_long", 57.9, 58),
    ])
    def test_score_bars_for_promoting_a_do_not_buy_row(self, env, board, key, below, at):
        env.rows["A"] = row("A", "DO NOT BUY", 10, horizons={board: {"score": below}})
        assert "horizon_focus" not in env.result(["A"])[key][0]         # fallback only
        env.store.clear()
        env.rows["A"] = row("A", "DO NOT BUY", 10, horizons={board: {"score": at}})
        picked = env.result(["A"])[key][0]
        assert picked["horizon_focus"] == board and picked["decision"] == "PREPARE TO BUY"
        assert picked["promoted_from_score"] is True

    def test_actionable_rows_are_kept_regardless_of_score_and_not_relabelled(self, env):
        env.rows["A"] = buy("A", 5)
        pick = env.result(["A"])["recommendations_short"][0]
        assert pick["horizon_focus"] == "short" and pick["decision"] == "BUY NOW"
        assert "promoted_from_score" not in pick

    def test_finding_sell_and_hold_rows_above_the_bar_land_on_the_boards_unchanged(self, env):
        """NOT FIXED (low): only DO NOT BUY is promoted; any other decision with a score at or above
        the bar (SELL, HOLD) is added as-is, so a SELL can sit in a "Top picks" list."""
        env.rows["A"] = row("A", "SELL", 80)
        env.rows["B"] = row("B", "HOLD", 70)
        recs = env.result(["A", "B"])["recommendations_short"]
        assert [(r["symbol"], r["decision"]) for r in recs] == [("A", "SELL"), ("B", "HOLD")]

    def test_each_board_holds_at_most_five(self, env):
        for i in range(7):
            env.rows[f"S{i:02d}"] = buy(f"S{i:02d}", 70 + i)
        res = env.result(syms(7))
        assert [r["symbol"] for r in res["recommendations_short"]] == ["S06", "S05", "S04", "S03", "S02"]

    def test_short_fallback_is_the_value_adjusted_pick_list_when_the_board_is_empty(self, env):
        env.rows["A"] = buy("A", 90, horizons={"short": {"score": 10, "decision": "HOLD"}})
        res = env.result(["A"])
        assert [r["symbol"] for r in res["recommendations_short"]] == ["A"]
        assert "horizon_focus" not in res["recommendations_short"][0]      # the raw top pick
        assert res["verdict"] == "1 strong opportunity(ies) found"

    @pytest.mark.parametrize("n, short, mid, long_", [
        (3, [0, 1, 2], [0, 1, 2], [0, 1, 2]),
        (12, [0, 1, 2, 3, 4], [5, 6, 7, 8, 9], [0, 1, 2, 3, 4]),
        (20, [0, 1, 2, 3, 4], [5, 6, 7, 8, 9], [10, 11, 12, 13, 14]),
    ])
    def test_weak_day_fallback_slices_the_ranked_results_into_bands(self, env, n, short, mid, long_):
        for i in range(n):
            env.rows[f"S{i:02d}"] = row(f"S{i:02d}", "HOLD", 40 - i)       # all under every bar
        res = env.result(syms(n))
        rank = [f"S{i:02d}" for i in range(n)]
        assert [r["symbol"] for r in res["recommendations_short"]] == [rank[i] for i in short]
        assert [r["symbol"] for r in res["recommendations_mid"]] == [rank[i] for i in mid]
        assert [r["symbol"] for r in res["recommendations_long"]] == [rank[i] for i in long_]

    def test_an_error_row_inside_results_is_skipped_by_every_board(self, env, monkeypatch):
        """Unreachable in production (the classifier diverts ERROR rows to `errors`); reached by
        stubbing `run_in_batches` so the defensive skips in the boards and the fallback run."""
        good, bad = buy("GOOD", 70), row("BAD", "ERROR", 99)

        async def fake_batches(items, worker, **kw):
            return BatchResult(results=[bad, good], errors=[], processed=2)

        monkeypatch.setattr(gw, "run_in_batches", fake_batches)
        res = env.result(["GOOD", "BAD"])
        for key in ("recommendations_short", "recommendations_mid", "recommendations_long"):
            assert [r["symbol"] for r in res[key]] == ["GOOD"]

    def test_market_mood_ladder(self, env):
        cases = [
            ({"B1": "BUY NOW", "B2": "PREPARE TO BUY", "B3": "BUY NOW", "B4": "BUY NOW", "B5": "BUY NOW",
              "S1": "SELL", "S2": "SELL", "S3": "SELL", "S4": "SELL", "S5": "SELL", "S6": "SELL"}, "Bullish"),
            ({"B1": "BUY NOW", "S1": "SELL", "S2": "SELL"}, "Bearish"),
            ({"B1": "BUY NOW", "S1": "SELL"}, "Selective"),
            ({"B1": "BUY NOW"}, "Selective"),
            ({"H1": "HOLD", "D1": "DO NOT BUY"}, "Cautious"),
            ({}, "Cautious"),
        ]
        for decisions, mood in cases:
            env.store.clear()
            env.rows = {s: row(s, d, 60) for s, d in decisions.items()}
            assert env.result(list(decisions))["market_mood"] == mood, decisions

    def test_market_stats_count_each_decision_bucket(self, env):
        env.rows = {"A": row("A", "BUY NOW"), "B": row("B", "PREPARE TO BUY"), "C": row("C", "SELL"),
                    "D": row("D", "HOLD"), "E": row("E", "DO NOT BUY"), "F": row("F", "WATCH")}
        assert env.result(list("ABCDEF"))["market_stats"] == {
            "buy_signals": 2, "sell_signals": 1, "hold_signals": 1, "cautious": 2}


# ── after the scan: last-scan cache, metrics, notification, failures ────────

class TestFinish:
    def test_last_scan_record_is_written_with_its_own_ttl_when_there_are_results(self, env):
        env.run(syms(3))
        key, last, ttl = [s for s in env.sets if s[0] == gw.LAST_FULL_SCAN_KEY][0]
        assert ttl == gw.LAST_FULL_SCAN_TTL
        assert last["task_id"] == env.TASK and last["partial"] is False and last["cancelled"] is False
        assert last["processed"] == last["total"] == last["universe_size"] == 3
        assert last["scanned_at"] == last["result"]["scanned_at"]

    def test_all_error_scan_writes_no_last_scan_record_but_still_notifies(self, env):
        env.rows = {s: row(s, "ERROR", error="x") for s in syms(2)}
        payload = env.run(syms(2))
        assert payload["status"] == "done" and payload["result"]["scanned"] == 0
        assert gw.LAST_FULL_SCAN_KEY not in env.store
        assert len(env.notify_calls) == 1

    def test_notification_gets_recommendations_verdict_scanned_and_universe_size(self, env):
        env.rows["S00"] = buy("S00", 80)
        env.run(syms(4))
        recs, verdict, scanned, universe = env.notify_calls[0]
        assert [r["symbol"] for r in recs] == ["S00"]
        assert verdict == "1 strong opportunity(ies) found" and scanned == 4 and universe == 4

    def test_metrics_are_recorded_and_a_metrics_failure_is_swallowed(self, env):
        env.run(syms(3))
        assert env.metrics.incs == ["stockky_scan_complete_total"]
        assert ("stockky_last_scan_symbols", 3.0) in env.metrics.gauges
        assert any(n == "stockky_last_scan_elapsed_sec" for n, _ in env.metrics.gauges)
        env.metrics.raise_on = True
        assert env.run(syms(3))["status"] == "done"
        assert len(env.notify_calls) == 2                        # still got as far as the notification

    def test_finding_a_failing_notification_escapes_the_scan_after_the_result_is_saved(self, env):
        """NOT FIXED (low): `_send_scan_notification` is awaited without a guard, so any error in it
        (pass 61 pinned that a non-numeric `close` raises) propagates out of the background task.
        The done record and last-scan cache are already written, so the damage is a logged traceback."""
        env.notify_raises = ValueError("Unknown format code 'f' for object of type 'str'")
        with pytest.raises(ValueError):
            env.run(syms(2))
        assert env.store[env.task_key]["status"] == "done"
        assert gw.LAST_FULL_SCAN_KEY in env.store and gw._SCAN_IN_PROGRESS is False

    def test_finding_a_null_combined_score_crashes_the_sort_and_leaves_the_scan_flag_stuck_on(self, env):
        """NOT FIXED (medium): `results.sort(key=lambda r: r.get("combined_score", 0))` only
        defaults an ABSENT key; an explicit null (decision-engine JSON null merged over the default
        row) compares None with a number and raises TypeError. There is no try/finally, so
        `_SCAN_IN_PROGRESS` stays True (WS quote upstream stays paused) and the task record stays
        "running" forever — the background task swallows the traceback."""
        env.rows["A"] = row("A", "BUY NOW", None)
        env.rows["B"] = row("B", "HOLD", 40)
        with pytest.raises(TypeError):
            env.run(["A", "B"])
        assert gw._SCAN_IN_PROGRESS is True and gw.scan_in_progress() is True
        assert env.store[env.task_key]["status"] == "running"
        assert env.notify_calls == [] and gw.LAST_FULL_SCAN_KEY not in env.store


# ── _get_nifty50_data ────────────────────────────────────────────────────────

class FixedDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 30, 10, 0, 0, tzinfo=tz)


CACHE_KEY = f"{gw.MARKET_MOVERS_CACHE_PREFIX}2026-09-30"


def hist(closes, volumes=None, highs=None, lows=None):
    n = len(closes)
    return pd.DataFrame({
        "Close": closes,
        "Volume": volumes or [1000] * n,
        "High": highs or [c + 1 for c in closes],
        "Low": lows or [c - 1 for c in closes],
    })


class Movers:
    def __init__(self, mp):
        self.store, self.sets = {}, []
        self.phase = "open"
        self.indices = [f"N{i:02d}" for i in range(50)] + [f"R{i:03d}" for i in range(130)]
        self.hists = {}                 # sym -> DataFrame | Exception
        self.tickers_built = []
        self.resolved_none = set()
        self.indices_calls = 0
        self.pre_lock_hook = None
        self.log = RecLogger()
        mp.setattr(gw, "datetime", FixedDT)
        mp.setattr(gw, "_redis_get", lambda k: self.store.get(k))
        mp.setattr(gw, "_redis_set", self._set)
        mp.setattr(gw, "_market_session_phase_ist", lambda: self.phase)
        mp.setattr(gw, "_get_nifty_indices", self._indices)
        mp.setattr(gw, "resolve_ns_ticker", lambda s: None if s in self.resolved_none else s + ".NS")
        mp.setattr(gw.random, "shuffle", lambda x: None)
        mp.setattr(gw.yf, "Ticker", self._ticker)
        mp.setattr(gw, "MAX_PARALLEL_WORKERS", 4)
        mp.setattr(gw, "logger", self.log)

    def _set(self, key, value, ttl=None):
        self.sets.append((key, value, ttl))
        self.store[key] = value

    def _indices(self):
        self.indices_calls += 1
        if self.pre_lock_hook:
            self.pre_lock_hook()
        return list(self.indices)

    def _ticker(self, yf_symbol):
        self.tickers_built.append(yf_symbol)
        sym = yf_symbol[:-3]
        movers = self

        class T:
            def history(self, period, interval):
                spec = movers.hists.get(sym, hist([100.0, 101.0]))
                if isinstance(spec, BaseException):
                    raise spec
                return spec
        return T()


@pytest.fixture
def mv(monkeypatch):
    return Movers(monkeypatch)


class TestNifty50Data:
    @pytest.mark.parametrize("phase", ["preopen", "closed", "open"])
    def test_a_non_empty_cached_list_is_served_without_touching_anything_else(self, mv, phase):
        mv.phase = phase
        mv.store[CACHE_KEY] = [{"symbol": "X", "change_pct": 1.0}]
        assert gw._get_nifty50_data() == [{"symbol": "X", "change_pct": 1.0}]
        assert mv.indices_calls == 0 and mv.tickers_built == []

    @pytest.mark.parametrize("bad", [[], None, {"symbol": "X"}, "text"])
    def test_empty_or_wrong_typed_cache_entries_are_misses(self, mv, bad):
        mv.phase = "closed"
        mv.store[CACHE_KEY] = bad
        assert gw._get_nifty50_data() == []

    @pytest.mark.parametrize("phase", ["preopen", "closed"])
    def test_outside_the_open_session_the_last_known_list_is_served_and_yahoo_is_not_called(self, mv, phase):
        mv.phase = phase
        mv.store[gw.MARKET_MOVERS_LAST_KNOWN] = [{"symbol": "OLD", "change_pct": 2.0}]
        assert gw._get_nifty50_data() == [{"symbol": "OLD", "change_pct": 2.0}]
        assert mv.indices_calls == 0 and mv.tickers_built == []
        assert mv.log.any("info", f"Market session phase={phase} — serving last-known movers")

    @pytest.mark.parametrize("last", [None, [], "text", {"a": 1}])
    def test_outside_the_open_session_with_nothing_known_returns_empty_without_fetching(self, mv, last):
        mv.phase = "preopen"
        if last is not None:
            mv.store[gw.MARKET_MOVERS_LAST_KNOWN] = last
        assert gw._get_nifty50_data() == []
        assert mv.tickers_built == []
        assert mv.log.any("info", "no last-known movers cached yet")
        assert mv.sets == []                                    # nothing written either

    def test_open_session_fetches_nifty50_plus_the_first_hundred_of_the_tail_deduplicated(self, mv):
        mv.indices = [f"N{i:02d}" for i in range(50)] + ["N03"] + [f"R{i:03d}" for i in range(130)]
        data = gw._get_nifty50_data()
        # tail slice is the first 100 of (N03 + R000..R129) = N03 + R000..R098; N03 dedupes away
        assert len(mv.tickers_built) == 50 + 99
        assert [d["symbol"] for d in data][:3] == ["N00", "N01", "N02"]
        assert data[-1]["symbol"] == "R098"
        assert len({d["symbol"] for d in data}) == len(data)

    def test_row_maths_and_rounding(self, mv):
        mv.indices = ["ABC"]
        mv.hists["ABC"] = hist([100.0, 101.234, 103.456], volumes=[900, 500, 200],
                               highs=[101, 102.111, 104.999], lows=[99, 100.5, 102.005])
        assert gw._get_nifty50_data() == [{
            "symbol": "ABC", "price": 103.46, "change": 3.46, "change_pct": 3.46,
            "volume": 200, "high": 105.0, "low": 102.0,
        }]

    def test_finding_volume_high_low_are_the_last_one_minute_bar_and_change_is_since_the_first_bar(self, mv):
        """NOT FIXED (medium): `interval="1m"` history is used as if it were a daily bar. `volume`,
        `high` and `low` are the LAST MINUTE's values, and `change_pct` is measured from the first
        1-minute bar's CLOSE, not the previous session's close. /market/most-active therefore ranks
        by one minute of volume, and the gainers/losers boards by move since ~09:15."""
        mv.indices = ["BIGDAY", "QUIETNOW"]
        mv.hists["BIGDAY"] = hist([100, 101, 102], volumes=[900_000, 800_000, 10])
        mv.hists["QUIETNOW"] = hist([100, 100, 100], volumes=[5, 5, 50])
        by = {d["symbol"]: d for d in gw._get_nifty50_data()}
        assert by["BIGDAY"]["volume"] == 10 and by["QUIETNOW"]["volume"] == 50
        assert by["BIGDAY"]["high"] == 103 and by["BIGDAY"]["low"] == 101     # last bar only
        assert by["BIGDAY"]["change"] == 2      # vs first bar close (100), not yesterday's close
        assert sorted(by, key=lambda s: by[s]["volume"], reverse=True)[0] == "QUIETNOW"

    def test_symbols_that_fail_in_any_way_are_dropped_and_the_rest_kept_in_order(self, mv):
        mv.indices = ["A", "B", "C", "D", "E"]
        mv.resolved_none.add("B")                               # no yahoo ticker
        mv.hists["C"] = hist([])                                # empty frame
        mv.hists["D"] = RuntimeError("yahoo 429")               # raises
        data = gw._get_nifty50_data()
        assert [d["symbol"] for d in data] == ["A", "E"]
        assert mv.log.any("warning", "Could not fetch D: yahoo 429")

    def test_fresh_data_is_cached_for_a_day_and_kept_as_last_known_for_a_week(self, mv):
        mv.indices = ["A"]
        data = gw._get_nifty50_data()
        assert mv.sets == [(CACHE_KEY, data, 86400), (gw.MARKET_MOVERS_LAST_KNOWN, data, 7 * 86400)]

    def test_an_all_failed_fetch_caches_the_empty_list_but_does_not_replace_last_known(self, mv):
        mv.indices = ["A", "B"]
        mv.hists["A"] = mv.hists["B"] = RuntimeError("down")
        mv.store[gw.MARKET_MOVERS_LAST_KNOWN] = [{"symbol": "OLD"}]
        assert gw._get_nifty50_data() == []
        assert mv.sets == [(CACHE_KEY, [], 86400)]
        assert mv.store[gw.MARKET_MOVERS_LAST_KNOWN] == [{"symbol": "OLD"}]
        # an empty list is falsy, so the next request re-fetches instead of trusting it
        mv.hists.clear()
        assert [d["symbol"] for d in gw._get_nifty50_data()] == ["A", "B"]

    def test_cache_filled_while_waiting_for_the_lock_is_reused_without_fetching(self, mv):
        def fill():                                             # runs inside the locked section
            raise AssertionError("must not fetch")
        mv.pre_lock_hook = fill
        reads = {"n": 0}
        orig_get = gw._redis_get

        def get(key):
            if key == CACHE_KEY:
                reads["n"] += 1
                if reads["n"] >= 2:                            # the post-lock re-check
                    return [{"symbol": "FRESH", "change_pct": 1.0}]
                return None
            return orig_get(key)

        gw._redis_get = get
        try:
            assert gw._get_nifty50_data() == [{"symbol": "FRESH", "change_pct": 1.0}]
        finally:
            gw._redis_get = orig_get
        assert mv.indices_calls == 0 and mv.log.any("info", "(post-lock)")

    def test_concurrent_cold_callers_share_one_fetch(self, mv):
        entered, release = threading.Event(), threading.Event()

        def hold():
            entered.set()
            assert release.wait(10)
        mv.pre_lock_hook = hold
        mv.indices = ["A", "B"]
        out = {}

        def call(name):
            out[name] = gw._get_nifty50_data()

        t1 = threading.Thread(target=call, args=("one",))
        t2 = threading.Thread(target=call, args=("two",))
        t1.start()
        assert entered.wait(10)
        t2.start()
        time.sleep(0.2)                                         # let t2 reach the lock
        release.set()
        t1.join(10)
        t2.join(10)
        assert not t1.is_alive() and not t2.is_alive()
        assert mv.indices_calls == 1
        assert out["one"] == out["two"] and [d["symbol"] for d in out["one"]] == ["A", "B"]
