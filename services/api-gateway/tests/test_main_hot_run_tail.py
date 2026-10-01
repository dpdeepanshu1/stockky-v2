"""tests/test_main_hot_run_tail.py — coverage for api-gateway/main.py, slice 22 (lines 11537-11889) + 4656-4657

Pass 80 (the last main.py pass). The Hot Picks run / stop / table / notify / audit / repair routes, the OS signal
hooks, and the one leftover line pair in `get_stock_decision`:

* `POST /stockky-hot/run` — already-running guard, pre-run setup (stop-flag clear + schema), the running job row,
  and the `_run_hot` worker: cache clear, the throttled `progress_cb` (`_on_symbol`), result persistence, done /
  stopped / error rows, the `finally` stop-flag clear;
* `POST /stockky-hot/stop`, `GET /stockky-hot/table` (window clamping, section flattening, empty envelope);
* `POST /stockky-hot/notify-top-picks` — cache -> durable-table fallback, merge + sort order, message layout,
  top_n clamp, notification service result handling;
* `GET /stockky-hot/audit`, `POST /stockky-hot/repair-batch` — price pass + score pass, de-duplicated merge, the
  overall status rules;
* `_install_signal_handlers` and the handler it installs;
* `get_stock_decision`: the defensive "reasons is not a dict" reset (4656-4657).

Everything downstream is faked: the `hotpicks_store` module (via sys.modules), the redis get/set shims, the hot job
helpers, `stockky_hot_stocks`, `httpx.post`, `signal.signal`, the module clock. Nothing touches the network or a
database. Findings are pinned as current behaviour and marked ``NOT FIXED``.

Deliberately NOT covered (unreachable, left as dead code): main.py 4375-4376 (`except` around `list.append` in
`ops_qstash_tick`) and the `if not top:` guard in `api_hotpicks_notify_top_picks` (merged is never empty once
`_has_picks` passed).

Run from services/api-gateway:
    python3 -m pytest tests/test_main_hot_run_tail.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)

import httpx
from fastapi import BackgroundTasks


def _run(coro):
    return asyncio.run(coro)


# ── fakes ────────────────────────────────────────────────────────────────────

class HttpResp:
    def __init__(self, status=200, data=None, json_raises=False):
        self.status_code, self._data, self._raises = status, data, json_raises
        self.content = b"x"

    def json(self):
        if self._raises:
            raise ValueError("bad json")
        return self._data

    def raise_for_status(self):
        return None


class FakeRedisClient:
    def __init__(self, raises=False):
        self.deleted, self.raises = [], raises

    def delete(self, key):
        if self.raises:
            raise RuntimeError("redis down")
        self.deleted.append(key)


@pytest.fixture
def hh(monkeypatch):
    monkeypatch.delenv("HOT_PROGRESS_MIN_INTERVAL_SEC", raising=False)
    h = types.SimpleNamespace(
        redis={}, redis_sets=[], job={"status": "idle"}, job_sets=[], job_set_raises=None,
        store_calls=[], store_raises={}, payload=None, payload_calls=[], audit={"ok": True},
        price_res={"status": "ok", "repaired": [], "attempted": 0},
        score_res={"status": "ok", "repaired": [], "attempted": 0},
        price_calls=[], score_calls=[],
        stocks_result={"universe_size": 2, "processed_symbols": 2}, stocks_raises=None, stocks_calls=[],
        events=[], clock=[100.0],
    )

    def guard(name):
        h.store_calls.append(name)
        exc = h.store_raises.get(name)
        if exc:
            raise exc

    store = types.ModuleType("hotpicks_store")
    store.HOTPICKS_TABLE_HOURS = 24.0
    store.clear_hotpicks_stop = lambda: guard("clear_hotpicks_stop")
    store.ensure_hotpicks_schema = lambda: guard("ensure_hotpicks_schema")
    store.request_hotpicks_stop = lambda: guard("request_hotpicks_stop")

    def db_payload(hours=None):
        h.payload_calls.append(hours)
        exc = h.store_raises.get("hotpicks_db_payload")
        if exc:
            raise exc
        return h.payload

    def audit():
        h.store_calls.append("hotpicks_audit")
        return h.audit

    def repair_batch(limit=15, symbol=None, market_data_url=""):
        h.price_calls.append({"limit": limit, "symbol": symbol, "market_data_url": market_data_url})
        exc = h.store_raises.get("hotpicks_repair_batch")
        if exc:
            raise exc
        return h.price_res

    def repair_scores(limit=15, symbol=None, decision_url=""):
        h.score_calls.append({"limit": limit, "symbol": symbol, "decision_url": decision_url})
        exc = h.store_raises.get("hotpicks_repair_scores")
        if exc:
            raise exc
        return h.score_res

    store.hotpicks_db_payload = db_payload
    store.hotpicks_audit = audit
    store.hotpicks_repair_batch = repair_batch
    store.hotpicks_repair_scores = repair_scores
    h.store = store

    def redis_set(key, value, ttl=None):
        h.redis_sets.append((key, value, ttl))
        h.redis[key] = value

    def job_set(set_fn, get_fn, **kw):
        if h.job_set_raises and h.job_set_raises(kw):
            raise RuntimeError("job write failed")
        h.job_sets.append(kw)
        h.job = {**h.job, **kw}

    async def stocks(force=False, progress_cb=None):
        h.stocks_calls.append((force, progress_cb is not None))
        for ev in h.events:
            ev(progress_cb)
        if h.stocks_raises:
            raise h.stocks_raises
        return h.stocks_result

    monkeypatch.setitem(sys.modules, "hotpicks_store", store)
    monkeypatch.setattr(gw, "_redis_get", lambda k: h.redis.get(k))
    monkeypatch.setattr(gw, "_redis_set", redis_set)
    monkeypatch.setattr(gw, "hot_job_get", lambda g: dict(h.job))
    monkeypatch.setattr(gw, "hot_job_set", job_set)
    monkeypatch.setattr(gw, "stockky_hot_stocks", stocks)
    monkeypatch.setattr(gw, "_redis", None)
    monkeypatch.setattr(gw, "time", types.SimpleNamespace(time=lambda: h.clock[0]))
    return h


def no_store(monkeypatch):
    """Make `from hotpicks_store import <anything>` fail with ImportError."""
    monkeypatch.setitem(sys.modules, "hotpicks_store", types.ModuleType("hotpicks_store"))


def start(hh, **kw):
    bt = BackgroundTasks()
    out = _run(gw.stockky_hot_run(bt, **kw))
    return out, bt


def run_hot(hh, **setup):
    for k, v in setup.items():
        setattr(hh, k, v)
    out, bt = start(hh)
    hh.job_sets.clear()
    hh.store_calls.clear()
    hh.redis_sets.clear()
    t = bt.tasks[0]
    _run(t.func(*t.args, **t.kwargs))
    return out


def final(hh):
    return hh.job_sets[-1]


def scans(hh):
    return [kw for kw in hh.job_sets if str(kw.get("message", "")).startswith("Scanning")]


def tick(hh, dt):
    return lambda cb: hh.clock.__setitem__(0, hh.clock[0] + dt)


# ══ POST /stockky-hot/run ════════════════════════════════════════════════════

class TestHotRunRoute:
    def test_running_job_short_circuits(self, hh):
        hh.job = {"status": "running", "processed": 4}
        out, bt = start(hh)
        assert out == {"ok": True, "already_running": True, "status": "running", "processed": 4}
        assert bt.tasks == [] and hh.job_sets == [] and hh.store_calls == []

    @pytest.mark.parametrize("status", ["idle", "done", "stopped", "error", None])
    def test_non_running_status_starts(self, hh, status):
        hh.job = {"status": status}
        assert start(hh)[0] == {"ok": True, "started": True, "message": "Hot Picks search started"}

    def test_start_clears_the_stop_flag_then_ensures_the_schema(self, hh):
        start(hh)
        assert hh.store_calls == ["clear_hotpicks_stop", "ensure_hotpicks_schema"]

    def test_start_writes_a_running_job_and_queues_one_worker(self, hh):
        out, bt = start(hh)
        job = hh.job_sets[0]
        assert job["status"] == "running" and job["processed"] == 0 and job["total"] == 0
        assert job["message"] == "Building catalyst universe…"
        assert job["estimated_remaining_sec"] is None and job["finished_at"] is None
        assert job["current_symbol"] is None and job["stopped"] is False
        assert job["started_at"].endswith("+05:30")
        assert len(bt.tasks) == 1 and bt.tasks[0].args == ()

    def test_schema_failure_is_swallowed(self, hh):
        hh.store_raises["ensure_hotpicks_schema"] = RuntimeError("no db")
        out, bt = start(hh)
        assert out["started"] is True and len(bt.tasks) == 1 and hh.job_sets[0]["status"] == "running"

    def test_clear_failure_skips_the_schema_step_but_still_starts(self, hh):
        hh.store_raises["clear_hotpicks_stop"] = RuntimeError("no db")
        out, bt = start(hh)
        assert out["started"] is True and hh.store_calls == ["clear_hotpicks_stop"]

    def test_missing_store_module_is_swallowed(self, hh, monkeypatch):
        no_store(monkeypatch)
        out, bt = start(hh)
        assert out["started"] is True and len(bt.tasks) == 1

    def test_force_flag_is_accepted_but_ignored(self, hh):
        """NOT FIXED: `force` is never read — the worker always scans with force=True."""
        out, bt = start(hh, force=False)
        t = bt.tasks[0]
        _run(t.func(*t.args, **t.kwargs))
        assert hh.stocks_calls == [(True, True)]


# ══ _run_hot worker: outcomes ════════════════════════════════════════════════

class TestHotRunWorkerOutcomes:
    RESULT = {"universe_size": 10, "processed_symbols": 8, "news_driven": [1, 2], "results_driven": [3],
              "bulk_insider_driven": [], "generated_at": "G0"}

    def test_done_row_counts_picks_and_reports_real_progress(self, hh):
        run_hot(hh, stocks_result=dict(self.RESULT))
        job = final(hh)
        assert job["status"] == "done" and job["processed"] == 8 and job["total"] == 10
        assert job["current_symbol"] is None and job["stopped"] is False and job["estimated_remaining_sec"] == 0
        assert job["message"] == f"Hot Picks ready at {job['finished_at']} — 3 pick(s)"
        assert job["finished_at"].endswith("+05:30")

    def test_scan_runs_with_force_and_a_progress_callback(self, hh):
        run_hot(hh)
        assert hh.stocks_calls == [(True, True)]

    def test_result_is_persisted_with_a_20h_ttl_and_keeps_its_generated_at(self, hh):
        run_hot(hh, stocks_result=dict(self.RESULT))
        (key, value, ttl), = hh.redis_sets
        assert key == gw.HOT_RESULT_KEY and ttl == 20 * 3600
        assert value["generated_at"] == "G0" and value["persisted_at"] == final(hh)["finished_at"]
        assert value["news_driven"] == [1, 2]

    def test_missing_generated_at_is_stamped_with_the_finish_time(self, hh):
        run_hot(hh, stocks_result={"universe_size": 1, "processed_symbols": 1})
        value = hh.redis_sets[0][1]
        assert value["generated_at"] == value["persisted_at"] == final(hh)["finished_at"]

    def test_processed_falls_back_to_the_universe_size(self, hh):
        run_hot(hh, stocks_result={"universe_size": 7})
        assert final(hh)["processed"] == 7 and final(hh)["total"] == 7

    def test_empty_result_dict_finishes_with_zeroes(self, hh):
        run_hot(hh, stocks_result={})
        job = final(hh)
        assert job["status"] == "done" and job["processed"] == 0 and job["total"] == 0
        assert job["message"].endswith("— 0 pick(s)")
        assert len(hh.redis_sets) == 1

    def test_none_result_is_not_persisted(self, hh):
        run_hot(hh, stocks_result=None)
        assert hh.redis_sets == [] and final(hh)["status"] == "done" and final(hh)["processed"] == 0

    def test_stopped_early_marks_the_job_stopped_and_keeps_the_picks(self, hh):
        run_hot(hh, stocks_result={**self.RESULT, "stopped_early": True, "processed_symbols": 4})
        job = final(hh)
        assert job["status"] == "stopped" and job["stopped"] is True
        assert job["message"] == "Stopped after 4/10 symbols — 3 pick(s) kept"
        assert job["processed"] == 4 and job["total"] == 10 and len(hh.redis_sets) == 1

    def test_stopped_message_shows_zero_total_when_the_universe_size_is_missing(self, hh):
        """NOT FIXED: the stopped message uses the raw `total` (0) while the job row uses `total or done`."""
        run_hot(hh, stocks_result={"stopped_early": True, "processed_symbols": 4})
        assert final(hh)["message"] == "Stopped after 4/0 symbols — 0 pick(s) kept"
        assert final(hh)["total"] == 4

    def test_scan_failure_lands_in_an_error_row_truncated_to_200(self, hh):
        run_hot(hh, stocks_raises=RuntimeError("E" * 300))
        job = final(hh)
        assert job["status"] == "error" and job["message"] == "E" * 200
        assert job["current_symbol"] is None and job["estimated_remaining_sec"] is None
        assert hh.redis_sets == []

    def test_non_dict_result_lands_in_the_error_row(self, hh):
        """NOT FIXED: a truthy non-dict scan result hits `(result or {}).get` and becomes an AttributeError."""
        run_hot(hh, stocks_result=["x"])
        assert final(hh)["status"] == "error" and "get" in final(hh)["message"]

    def test_stop_flag_is_cleared_after_success(self, hh):
        run_hot(hh)
        assert hh.store_calls == ["clear_hotpicks_stop"]

    def test_stop_flag_is_cleared_after_failure(self, hh):
        run_hot(hh, stocks_raises=RuntimeError("x"))
        assert hh.store_calls == ["clear_hotpicks_stop"]

    def test_stop_flag_clear_failure_is_swallowed(self, hh):
        run_hot(hh, store_raises={"clear_hotpicks_stop": RuntimeError("x")})
        assert final(hh)["status"] == "done"

    def test_short_cache_is_deleted_before_the_scan(self, hh, monkeypatch):
        fake = FakeRedisClient()
        monkeypatch.setattr(gw, "_redis", fake)
        run_hot(hh)
        assert fake.deleted == [gw.HOT_STOCKS_CACHE_KEY]

    def test_cache_delete_failure_is_swallowed(self, hh, monkeypatch):
        monkeypatch.setattr(gw, "_redis", FakeRedisClient(raises=True))
        run_hot(hh)
        assert final(hh)["status"] == "done" and hh.stocks_calls == [(True, True)]

    def test_no_redis_client_skips_the_delete(self, hh):
        run_hot(hh)
        assert final(hh)["status"] == "done"


# ══ _run_hot worker: throttled progress ══════════════════════════════════════

class TestHotRunProgress:
    def test_first_call_always_writes_with_real_progress(self, hh):
        hh.events = [lambda cb: cb(0, 100, "AAA")]
        run_hot(hh, events=hh.events)
        assert scans(hh) == [{"processed": 0, "total": 100, "current_symbol": "AAA",
                              "message": "Scanning AAA (1/100)"}]

    def test_writes_are_throttled_to_one_per_second_but_the_last_symbol_always_lands(self, hh):
        ev = [lambda cb: cb(0, 100, "AAA"),
              tick(hh, 0.4), lambda cb: cb(1, 100, "BBB"),        # < 1s since last write -> skipped
              tick(hh, 0.7), lambda cb: cb(2, 100, "CCC"),        # 1.1s -> written
              lambda cb: cb(3, 100, "DDD"),                       # same instant -> skipped
              lambda cb: cb(99, 100, "ZZZ")]                      # processed >= total - 1 -> forced
        run_hot(hh, events=ev)
        assert [s["processed"] for s in scans(hh)] == [0, 2, 99]
        assert [s["message"] for s in scans(hh)] == ["Scanning AAA (1/100)", "Scanning CCC (3/100)",
                                                     "Scanning ZZZ (100/100)"]

    def test_unknown_total_is_never_treated_as_last(self, hh):
        ev = [lambda cb: cb(0, 0, "A"), lambda cb: cb(1, 0, "B")]
        run_hot(hh, events=ev)
        assert [s["current_symbol"] for s in scans(hh)] == ["A"] and scans(hh)[0]["total"] == 0

    def test_zero_interval_env_writes_every_symbol(self, hh, monkeypatch):
        monkeypatch.setenv("HOT_PROGRESS_MIN_INTERVAL_SEC", "0")
        ev = [lambda cb: cb(0, 10, "A"), lambda cb: cb(1, 10, "B"), lambda cb: cb(2, 10, "C")]
        run_hot(hh, events=ev)
        assert [s["current_symbol"] for s in scans(hh)] == ["A", "B", "C"]

    def test_interval_env_is_honoured_and_the_boundary_writes(self, hh, monkeypatch):
        monkeypatch.setenv("HOT_PROGRESS_MIN_INTERVAL_SEC", "5")
        ev = [lambda cb: cb(0, 100, "A"),
              tick(hh, 4.0), lambda cb: cb(1, 100, "B"),
              tick(hh, 1.0), lambda cb: cb(2, 100, "C")]
        run_hot(hh, events=ev)
        assert [s["current_symbol"] for s in scans(hh)] == ["A", "C"]

    def test_values_are_coerced_and_the_batch_arg_is_accepted(self, hh):
        run_hot(hh, events=[lambda cb: cb(2, 10, 123, 4)])
        assert scans(hh)[0]["current_symbol"] == "123" and scans(hh)[0]["message"] == "Scanning 123 (3/10)"

    def test_progress_write_failure_never_breaks_the_scan(self, hh):
        hh.job_set_raises = lambda kw: kw.get("current_symbol") is not None
        run_hot(hh, events=[lambda cb: cb(0, 10, "A"), lambda cb: cb(9, 10, "Z")])
        assert final(hh)["status"] == "done"

    def test_bad_interval_env_kills_the_worker_and_leaves_the_job_running(self, hh, monkeypatch):
        """NOT FIXED: `float(os.getenv(...))` sits outside the try, so a bad value raises out of the background
        task — the job stays "running" forever and the stop flag is never cleared."""
        monkeypatch.setenv("HOT_PROGRESS_MIN_INTERVAL_SEC", "abc")
        out, bt = start(hh)
        hh.store_calls.clear()
        t = bt.tasks[0]
        with pytest.raises(ValueError):
            _run(t.func(*t.args, **t.kwargs))
        assert hh.job["status"] == "running" and hh.store_calls == []


# ══ POST /stockky-hot/stop ═══════════════════════════════════════════════════

class TestHotStop:
    def test_stop_unavailable_is_a_soft_error_truncated_to_160(self, hh):
        hh.store_raises["request_hotpicks_stop"] = RuntimeError("S" * 300)
        assert gw.stockky_hot_stop() == {"ok": False, "detail": "stop unavailable: " + "S" * 160}
        assert hh.job_sets == []

    def test_missing_store_module_is_a_soft_error(self, hh, monkeypatch):
        no_store(monkeypatch)
        out = gw.stockky_hot_stop()
        assert out["ok"] is False and out["detail"].startswith("stop unavailable: ")

    def test_running_job_gets_a_stop_request(self, hh):
        hh.job = {"status": "running"}
        out = gw.stockky_hot_stop()
        assert out == {"ok": True, "stopping": True, "message": "Stopping after the current symbol"}
        assert hh.store_calls == ["request_hotpicks_stop"]
        assert hh.job_sets == [{"message": "Stop requested — finishing current symbol…", "stop_requested": True}]

    def test_idle_job_reports_nothing_to_stop_and_writes_nothing(self, hh):
        hh.job = {"status": "idle", "processed": 0}
        out = gw.stockky_hot_stop()
        assert out == {"ok": True, "stopping": False, "message": "No Hot Picks scan is running",
                       "status": "idle", "processed": 0}
        assert hh.job_sets == []

    def test_the_flag_is_raised_even_when_nothing_is_running(self, hh):
        gw.stockky_hot_stop()
        assert hh.store_calls == ["request_hotpicks_stop"]

    def test_last_job_message_clobbers_the_nothing_running_message(self, hh):
        """NOT FIXED: `**job` is spread after `message`, so any finished job's own message replaces
        "No Hot Picks scan is running"."""
        hh.job = {"status": "done", "message": "Hot Picks ready at T — 3 pick(s)"}
        assert gw.stockky_hot_stop()["message"] == "Hot Picks ready at T — 3 pick(s)"


# ══ GET /stockky-hot/table ═══════════════════════════════════════════════════

class TestHotTable:
    def test_missing_store_module_is_a_soft_failure_echoing_the_raw_hours(self, hh, monkeypatch):
        no_store(monkeypatch)
        out = gw.stockky_hot_table(hours=12)
        assert out["ok"] is False and out["rows"] == [] and out["count"] == 0
        assert out["hours"] == 12 and out["fresh"] is False
        assert out["detail"].startswith("hotpicks store unavailable: ")

    @pytest.mark.parametrize("hours,window", [(5, 5), (0, 24), (None, 24), (-3, 1), (100000, 720), ("abc", 24),
                                              (2.9, 2)])
    def test_window_is_parsed_defaulted_and_clamped(self, hh, hours, window):
        gw.stockky_hot_table(hours=hours)
        assert hh.payload_calls == [window]

    def test_default_window_comes_from_the_store_constant(self, hh):
        hh.store.HOTPICKS_TABLE_HOURS = 12.5
        gw.stockky_hot_table(hours=0)
        assert hh.payload_calls == [12]

    @pytest.mark.parametrize("payload", [None, {}])
    def test_no_stored_picks_is_an_empty_ok_envelope(self, hh, payload):
        hh.payload = payload
        assert gw.stockky_hot_table(hours=6) == {
            "ok": True, "rows": [], "count": 0, "hours": 6, "fresh": False,
            "detail": "No stored Hot Picks in this window yet — run a scan."}

    def test_sections_are_flattened_in_news_results_bulk_order(self, hh):
        hh.payload = {"news_driven": [{"s": "N"}], "results_driven": [{"s": "R"}],
                      "bulk_insider_driven": [{"s": "B"}], "count": 3, "fresh": True, "rows": ["ignored"]}
        out = gw.stockky_hot_table(hours=24)
        assert out["rows"] == [{"s": "N"}, {"s": "R"}, {"s": "B"}]
        assert out["ok"] is True and out["count"] == 3 and out["fresh"] is True

    def test_none_sections_count_as_empty(self, hh):
        hh.payload = {"news_driven": None, "results_driven": [{"s": "R"}], "fresh": True}
        assert gw.stockky_hot_table(hours=24)["rows"] == [{"s": "R"}]

    def test_payload_keys_can_override_ok(self, hh):
        """NOT FIXED: the payload is spread after `ok=True`."""
        hh.payload = {"ok": False, "news_driven": []}
        assert gw.stockky_hot_table(hours=24)["ok"] is False


# ══ POST /stockky-hot/notify-top-picks ═══════════════════════════════════════

def pick(sym, decision="BUY NOW", score=50, **kw):
    return {"symbol": sym, "decision": decision, "score": score, **kw}


@pytest.fixture
def post(hh, monkeypatch):
    p = types.SimpleNamespace(calls=[], resp=HttpResp(200, {"delivered": True}), raises=None)

    def _post(url, json=None, timeout=None):
        p.calls.append((url, json, timeout))
        if p.raises:
            raise p.raises
        return p.resp

    monkeypatch.setattr(gw.httpx, "post", _post)
    monkeypatch.setattr(gw, "NOTIFICATION_URL", "http://n.t")
    return p


def notify(top_n=5):
    return _run(gw.api_hotpicks_notify_top_picks(top_n=top_n))


NO_PICKS = {"ok": False, "sent": False, "count": 0,
            "message": "No Hot Picks available right now — run a scan first."}

MIXED = {
    "bulk_insider_driven": [pick("BI1", "DO NOT BUY", 90, price=10, section="bulk_insider_driven")],
    "results_driven": [pick("R1", "BUY NOW", 50, close=20, section="results_driven")],
    "news_driven": [pick("N1", "buy now", 80, section="news_driven"), {"symbol": "N2", "score": None}],
}


class TestNotifyTopPicks:
    def test_no_cache_and_no_durable_rows(self, hh, post):
        assert notify() == NO_PICKS
        assert hh.payload_calls == [None] and post.calls == []

    def test_cache_wins_and_the_durable_table_is_not_read(self, hh, post):
        hh.redis[gw.HOT_STOCKS_CACHE_KEY] = {"news_driven": [pick("C1")]}
        assert notify()["symbols"] == ["C1"]
        assert hh.payload_calls == []

    def test_durable_table_is_the_fallback(self, hh, post):
        hh.payload = {"results_driven": [pick("D1")]}
        assert notify()["symbols"] == ["D1"]

    def test_durable_table_failure_is_treated_as_no_picks(self, hh, post):
        hh.store_raises["hotpicks_db_payload"] = RuntimeError("db down")
        assert notify() == NO_PICKS

    def test_missing_store_module_is_treated_as_no_picks(self, hh, post, monkeypatch):
        no_store(monkeypatch)
        assert notify() == NO_PICKS

    def test_non_dict_cache_is_not_a_result(self, hh, post):
        hh.redis[gw.HOT_STOCKS_CACHE_KEY] = ["x"]
        assert notify() == NO_PICKS

    def test_empty_sections_are_not_a_result(self, hh, post):
        hh.redis[gw.HOT_STOCKS_CACHE_KEY] = {"news_driven": [], "results_driven": None}
        assert notify() == NO_PICKS

    def test_an_empty_but_truthy_cache_blocks_the_durable_fallback(self, hh, post):
        """NOT FIXED: only a falsy cache falls through to hotpicks_static_feed, so a stale empty cache dict hides
        picks that are sitting in the durable table."""
        hh.redis[gw.HOT_STOCKS_CACHE_KEY] = {"news_driven": []}
        hh.payload = {"news_driven": [pick("D1")]}
        assert notify() == NO_PICKS and hh.payload_calls == []

    def test_sorted_by_decision_rank_then_score_desc(self, hh, post):
        hh.redis[gw.HOT_STOCKS_CACHE_KEY] = MIXED
        assert notify()["symbols"] == ["N1", "R1", "BI1", "N2"]

    def test_ties_keep_bulk_then_results_then_news_order(self, hh, post):
        hh.redis[gw.HOT_STOCKS_CACHE_KEY] = {
            "news_driven": [pick("N", score=70)], "results_driven": [pick("R", score=70)],
            "bulk_insider_driven": [pick("B", score=70)]}
        assert notify()["symbols"] == ["B", "R", "N"]

    def test_message_layout_and_fallbacks(self, hh, post):
        hh.redis[gw.HOT_STOCKS_CACHE_KEY] = MIXED
        notify()
        url, body, timeout = post.calls[0]
        assert url == "http://n.t/notify" and timeout == 15
        assert body["title"] == "Stockky Hot Picks — Top Picks" and body["channel"] == "telegram"
        assert body["message"] == "\n".join([
            "🔥 *Stockky Hot Picks — Top 4*",
            "1. N1 — buy now (score 80/100)\n   price n/a · news driven",
            "2. R1 — BUY NOW (score 50/100)\n   ₹20 · results driven",
            "3. BI1 — DO NOT BUY (score 90/100)\n   ₹10 · bulk insider driven",
            "4. N2 — — (score —/100)\n   price n/a · ",
        ])

    @pytest.mark.parametrize("top_n,count", [(2, 2), (0, 1), (99, 20)])
    def test_top_n_is_clamped_to_1_through_20(self, hh, post, top_n, count):
        hh.redis[gw.HOT_STOCKS_CACHE_KEY] = {"news_driven": [pick(f"S{i}", score=i) for i in range(25)]}
        out = notify(top_n)
        assert out["count"] == count and len(out["symbols"]) == count

    def test_delivered_true_reports_sent(self, hh, post):
        hh.redis[gw.HOT_STOCKS_CACHE_KEY] = {"news_driven": [pick("A")]}
        post.resp = HttpResp(200, {"delivered": True, "id": 7})
        assert notify() == {"ok": True, "sent": True, "count": 1, "symbols": ["A"],
                            "notification_result": {"delivered": True, "id": 7}}

    @pytest.mark.parametrize("detail", [{"delivered": False}, {}, ["x"]])
    def test_anything_but_delivered_true_is_not_sent(self, hh, post, detail):
        hh.redis[gw.HOT_STOCKS_CACHE_KEY] = {"news_driven": [pick("A")]}
        post.resp = HttpResp(200, detail)
        out = notify()
        assert out["ok"] is True and out["sent"] is False and out["notification_result"] == detail

    def test_bad_json_falls_back_to_the_status_code(self, hh, post):
        hh.redis[gw.HOT_STOCKS_CACHE_KEY] = {"news_driven": [pick("A")]}
        post.resp = HttpResp(502, json_raises=True)
        out = notify()
        assert out["sent"] is False and out["notification_result"] == {"status_code": 502}

    def test_error_status_is_still_ok_true(self, hh, post):
        """NOT FIXED: the notification service's HTTP status is never checked — a 500 with a JSON body comes back
        as ok=True, sent=False."""
        hh.redis[gw.HOT_STOCKS_CACHE_KEY] = {"news_driven": [pick("A")]}
        post.resp = HttpResp(500, {"detail": "boom"})
        out = notify()
        assert out["ok"] is True and out["sent"] is False

    def test_post_failure_is_a_soft_error_truncated_to_300(self, hh, post):
        hh.redis[gw.HOT_STOCKS_CACHE_KEY] = {"news_driven": [pick("A"), pick("B")]}
        post.raises = RuntimeError("N" * 400)
        assert notify() == {"ok": False, "sent": False, "count": 2, "error": "N" * 300}

    def test_post_runs_off_the_event_loop_thread(self, hh, post, monkeypatch):
        import threading
        seen = []
        monkeypatch.setattr(gw.httpx, "post", lambda url, json=None, timeout=None:
                            seen.append(threading.current_thread() is threading.main_thread())
                            or HttpResp(200, {"delivered": True}))
        hh.redis[gw.HOT_STOCKS_CACHE_KEY] = {"news_driven": [pick("A")]}
        notify()
        assert seen == [False]


# ══ GET /stockky-hot/audit ═══════════════════════════════════════════════════

class TestHotAudit:
    def test_passes_the_store_audit_through(self, hh):
        hh.audit = {"ok": True, "rows": 12, "issues": []}
        assert gw.stockky_hot_audit() == {"ok": True, "rows": 12, "issues": []}
        assert hh.store_calls == ["hotpicks_audit"]

    def test_missing_store_module_is_a_soft_failure(self, hh, monkeypatch):
        no_store(monkeypatch)
        out = gw.stockky_hot_audit()
        assert out["ok"] is False and out["issues"][0].startswith("hotpicks store unavailable: ")


# ══ POST /stockky-hot/repair-batch ═══════════════════════════════════════════

def repair(limit=15, symbol=None):
    return gw.stockky_hot_repair_batch(limit=limit, symbol=symbol)


class TestHotRepairBatch:
    def test_missing_store_module_is_an_error_envelope(self, hh, monkeypatch):
        no_store(monkeypatch)
        out = repair()
        assert out["status"] == "error" and out["error"].startswith("hotpicks store unavailable: ")

    def test_both_passes_get_limit_symbol_and_service_urls(self, hh, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_URL", "http://m.t")
        monkeypatch.setenv("DECISION_URL", "http://d.t")
        repair(limit=40, symbol="TCS")
        assert hh.price_calls == [{"limit": 40, "symbol": "TCS", "market_data_url": "http://m.t"}]
        assert hh.score_calls == [{"limit": 40, "symbol": "TCS", "decision_url": "http://d.t"}]

    def test_url_defaults_when_the_env_is_unset(self, hh, monkeypatch):
        monkeypatch.delenv("MARKET_DATA_URL", raising=False)
        monkeypatch.delenv("DECISION_URL", raising=False)
        repair()
        assert hh.price_calls[0]["market_data_url"] == ""
        assert hh.score_calls[0]["decision_url"] == gw.DECISION_URL

    def test_merge_dedupes_in_order_and_sums_attempts(self, hh):
        hh.price_res = {"status": "ok", "repaired": ["A", "B"], "attempted": 3}
        hh.score_res = {"status": "ok", "repaired": ["B", "C"], "attempted": 2}
        out = repair()
        assert out == {"status": "completed", "repaired": ["A", "B", "C"], "price_repaired": ["A", "B"],
                       "score_repaired": ["B", "C"], "attempted": 5,
                       "price_detail": hh.price_res, "score_detail": hh.score_res}

    def test_missing_lists_and_counts_default_to_empty_and_zero(self, hh):
        hh.price_res = {"status": "ok"}
        hh.score_res = {"status": "ok", "repaired": None, "attempted": None}
        out = repair()
        assert out["repaired"] == [] and out["attempted"] == 0

    @pytest.mark.parametrize("price,score,score_repaired,expected", [
        ("error", "error", [], "error"),
        ("error", "ok", [], "completed"),
        ("ok", "error", [], "completed"),
        ("not_found", "not_found", [], "not_found"),
        ("not_found", "no_data", [], "not_found"),
        ("not_found", "not_found", ["X"], "completed"),
        ("not_found", "ok", [], "completed"),
        ("ok", "not_found", [], "completed"),
        ("no_data", "not_found", [], "completed"),
        ("ok", "ok", [], "completed"),
    ])
    def test_overall_status_rules(self, hh, price, score, score_repaired, expected):
        hh.price_res = {"status": price}
        hh.score_res = {"status": score, "repaired": score_repaired}
        assert repair()["status"] == expected

    def test_score_pass_failure_is_isolated_and_truncated_to_160(self, hh):
        hh.price_res = {"status": "ok", "repaired": ["A"]}
        hh.store_raises["hotpicks_repair_scores"] = RuntimeError("S" * 300)
        out = repair()
        assert out["score_detail"] == {"status": "error", "error": "S" * 160}
        assert out["status"] == "completed" and out["repaired"] == ["A"]

    def test_score_failure_plus_price_error_is_an_overall_error(self, hh):
        hh.price_res = {"status": "error"}
        hh.store_raises["hotpicks_repair_scores"] = RuntimeError("x")
        assert repair()["status"] == "error"

    def test_price_pass_failure_propagates_and_skips_the_score_pass(self, hh):
        """NOT FIXED: only the score pass is wrapped in try/except; an exception in the price pass is an
        unhandled 500 and the score repair never runs."""
        hh.store_raises["hotpicks_repair_batch"] = RuntimeError("price boom")
        with pytest.raises(RuntimeError):
            repair()
        assert hh.score_calls == []


# ══ _install_signal_handlers ═════════════════════════════════════════════════

@pytest.fixture
def sig(monkeypatch):
    import signal
    s = types.SimpleNamespace(calls=[], raise_for=set(), commits=[], commit_raises=None, signal=signal)

    def fake_signal(num, handler):
        s.calls.append((num, handler))
        if num in s.raise_for:
            raise OSError("not allowed")

    def commit(reason="shutdown"):
        s.commits.append(reason)
        if s.commit_raises:
            raise s.commit_raises
        return []

    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr(gw, "_graceful_shutdown_commit", commit)
    monkeypatch.setattr(gw._install_signal_handlers, "_installed", False, raising=False)
    return s


class TestSignalHandlers:
    def test_installs_sigterm_and_sigint_handlers_once(self, sig):
        gw._install_signal_handlers()
        assert [c[0] for c in sig.calls] == [sig.signal.SIGTERM, sig.signal.SIGINT]
        assert gw._install_signal_handlers._installed is True
        gw._install_signal_handlers()
        assert len(sig.calls) == 2

    def test_one_failing_install_does_not_block_the_other(self, sig):
        sig.raise_for = {sig.signal.SIGTERM}
        gw._install_signal_handlers()
        assert [c[0] for c in sig.calls] == [sig.signal.SIGTERM, sig.signal.SIGINT]

    def test_handler_runs_the_graceful_shutdown_commit_with_the_signal_name(self, sig):
        gw._install_signal_handlers()
        handlers = dict(sig.calls)
        handlers[sig.signal.SIGTERM](sig.signal.SIGTERM, None)
        handlers[sig.signal.SIGINT](sig.signal.SIGINT, None)
        assert sig.commits == ["signal_SIGTERM", "signal_SIGINT"]

    def test_commit_failure_inside_the_handler_is_swallowed(self, sig):
        sig.commit_raises = RuntimeError("commit failed")
        gw._install_signal_handlers()
        dict(sig.calls)[sig.signal.SIGTERM](sig.signal.SIGTERM, None)
        assert sig.commits == ["signal_SIGTERM"]

    def test_handler_falls_back_to_the_number_when_signal_has_no_Signals_enum(self, sig, monkeypatch):
        gw._install_signal_handlers()
        handler = dict(sig.calls)[sig.signal.SIGTERM]
        monkeypatch.delattr(sig.signal, "Signals")
        handler(15, None)
        assert sig.commits == ["signal_15"]


# ══ get_stock_decision: defensive reasons reset (4656-4657) ══════════════════

class _FlakyReasons(dict):
    """A decision dict whose `reasons` is a dict for the isinstance check and a non-dict for the value read."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._n = 0

    def get(self, key, default=None):
        if key == "reasons":
            self._n += 1
            if self._n == 1:
                return {"technical": ["ok"]}
            if self._n == 2:
                return "not-a-dict"
        return super().get(key, default)


class _DecideClient:
    def __init__(self):
        self.calls = []

    async def get(self, url, **kw):
        self.calls.append(url)
        return HttpResp(200, {"x": 1})


class _CM:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *a):
        return False


class TestStockDecisionReasonsReset:
    def test_non_dict_reasons_is_reset_to_an_empty_dict(self, monkeypatch):
        client = _DecideClient()
        base = {"decision": "BUY", "close": 100.0, "support": 95.0, "resistance": 110.0, "technical_score": 70,
                "fundamental_score": 68, "fundamental_metrics": {"pe": 20}, "news_score": 60,
                "prediction_score": 64, "training_score": 62, "event_data": {"x": 1}}

        async def ai(result, c):
            return "S"

        monkeypatch.setattr(gw.httpx, "AsyncClient", lambda *a, **k: _CM(client))
        monkeypatch.setattr(gw, "_resolve_symbol", lambda o: o.upper())
        monkeypatch.setattr(gw, "_add_searched", lambda s: None)
        monkeypatch.setattr(gw, "_redis", None)
        monkeypatch.setattr(gw, "_fetch_price_from_quote", lambda s: None)
        monkeypatch.setattr(gw, "_generate_ai_summary", ai)
        monkeypatch.setattr(gw, "_normalize_decision_response", lambda raw, sym: _FlakyReasons(base))
        out = _run(gw.get_stock_decision("tcs"))
        assert out["reasons"] == {}
        assert out["enrichment"]["need_events"] is False and client.calls == [f"{gw.DECISION_URL}/decide/TCS"]
