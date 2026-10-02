"""tests/test_main_catalyst_ws_quotes.py — coverage for api-gateway/main.py, slice 13 (lines 8232-8628)

Pass 71. The Hot Picks HTTP entry, the Catalyst Alert routes, the WebSocket connection hub and the WS quote
resolver:

* `GET /stockky-hot` — `force` (inline run), cache hit, cold cache (background warm-up via `/stockky-hot/run`,
  stale result, "warming" placeholder, failing warm-up, no `BackgroundTasks`);
* `GET /catalysts/alert/status` — idle / non-dict state, the 5-minute stale-heartbeat auto-heal;
* `GET|POST /catalysts/alert` — batch-size clamping, the already-running guard, the job record, the background
  vs `sync` run, pick selection / ranking / de-duplication, the Telegram message, the `/notify` post and its
  `send_picks_to_telegram` fallback, the keep-alive loop, the error path;
* `ConnectionManager` (`ws_manager`) — connect / disconnect, channel subscriptions, quote watch-lists,
  broadcast with dead-socket pruning;
* `_resolve_quote_price` — the market-data hit and every fall-through to the yfinance fast-info path.

The routes are called directly (no TestClient). Faked: the kv layer, `stockky_hot_stocks`, `stockky_hot_run`,
`_warm_upstream_services`, `_get_http_client`, `send_picks_to_telegram`, `httpx.get`, `yf.Ticker`,
`resolve_ns_ticker`, and (for the keep-alive loop) `asyncio.wait_for`. Nothing touches the network, a database
or a real WebSocket. Findings are pinned as current behaviour and marked ``NOT FIXED``; fixed ones say ``Fixed``.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_catalyst_ws_quotes.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta

import pytest
from fastapi import BackgroundTasks

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)

JOB_KEY = "stockky:catalyst_job"


def _run(coro):
    return asyncio.run(coro)


def _ago(seconds, naive=False, z=False):
    ts = datetime.now(gw.IST) - timedelta(seconds=seconds)
    if naive:
        return ts.replace(tzinfo=None).isoformat()
    if z:
        return ts.astimezone(gw.IST).isoformat().replace("+05:30", "Z")
    return ts.isoformat()


# ═════════════════════════════════════════════════════════════════════════════
# Fakes
# ═════════════════════════════════════════════════════════════════════════════

class KV:
    def __init__(self):
        self.store = {}
        self.sets = []
        self.set_raises = False

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl=None):
        if self.set_raises:
            raise RuntimeError("kv write down")
        self.sets.append((key, dict(value) if isinstance(value, dict) else value, ttl))
        self.store[key] = value


@pytest.fixture
def kv(monkeypatch):
    k = KV()
    monkeypatch.setattr(gw, "_redis_get", k.get)
    monkeypatch.setattr(gw, "_redis_set", k.set)
    return k


class _Resp:
    def __init__(self, status=200):
        self.status_code = status


class _Client:
    def __init__(self, env):
        self.env = env

    async def post(self, url, json=None):
        self.env.posts.append((url, json))
        if self.env.post_raises:
            raise self.env.post_raises
        return _Resp(self.env.post_status)


class _WaitProxy:
    """Stands in for `asyncio` inside main: lets a test force the keep-alive / final waits to time out."""

    def __init__(self, env):
        self._env = env

    async def wait_for(self, aw, timeout=None):
        if timeout == 75.0 and self._env.keepalive_timeouts > 0:
            self._env.keepalive_timeouts -= 1
            aw.close()
            raise asyncio.TimeoutError()
        if timeout == 2 and self._env.final_wait_times_out:
            raise asyncio.TimeoutError()
        return await asyncio.wait_for(aw, timeout=timeout)

    def __getattr__(self, name):
        return getattr(asyncio, name)


class CEnv:
    def __init__(self):
        self.hot = {"universe_size": 120, "news_driven": [], "results_driven": [], "bulk_insider_driven": []}
        self.hot_raises = None
        self.hot_calls = []
        self.hot_yields = 0                 # how many times the fake hot scan yields to the loop
        self.progress_probe = None          # (i, total, sym, batch) -> cb is invoked inside the fake scan
        self.corrupt_job = False            # overwrite the stored job with junk just before the callback
        self.job_during_hot = None
        self.run_calls = []
        self.run_raises = None
        self.warm_calls = 0
        self.warm_raises = False
        self.posts = []
        self.post_raises = None
        self.post_status = 200
        self.telegram_calls = []
        self.telegram_raises = None
        self.keepalive_timeouts = 0
        self.final_wait_times_out = False


@pytest.fixture
def cenv(monkeypatch, kv):
    env = CEnv()

    async def hot(force=False, max_symbols=None, progress_cb=None):
        env.hot_calls.append({"force": force, "max_symbols": max_symbols, "progress_cb": progress_cb,
                              "env_batch": os.environ.get("HOT_BATCH_SIZE")})
        if env.progress_probe and progress_cb:
            if env.corrupt_job:
                kv.store[JOB_KEY] = "junk"
            progress_cb(*env.progress_probe[:3], batch=env.progress_probe[3])
            env.job_during_hot = dict(kv.store.get(JOB_KEY) or {})
        for _ in range(env.hot_yields):
            await asyncio.sleep(0)
        if env.hot_raises:
            raise env.hot_raises
        return env.hot

    async def hot_run(background_tasks, force=True):
        env.run_calls.append((background_tasks, force))
        if env.run_raises:
            raise env.run_raises
        return {"ok": True}

    async def warm(client=None):
        env.warm_calls += 1
        if env.warm_raises:
            raise RuntimeError("warm down")

    def telegram(payload):
        env.telegram_calls.append(payload)
        if env.telegram_raises:
            raise env.telegram_raises

    monkeypatch.setattr(gw, "stockky_hot_stocks", hot)
    monkeypatch.setattr(gw, "stockky_hot_run", hot_run)
    monkeypatch.setattr(gw, "_warm_upstream_services", warm)
    monkeypatch.setattr(gw, "_get_http_client", lambda: _Client(env))
    monkeypatch.setattr(gw, "send_picks_to_telegram", telegram)
    monkeypatch.setattr(gw, "NOTIFICATION_URL", "http://notif.local/notification/")
    monkeypatch.setattr(gw, "asyncio", _WaitProxy(env))
    # _work writes os.environ["HOT_BATCH_SIZE"]; register it so teardown restores the original state
    monkeypatch.setenv("HOT_BATCH_SIZE", "orig")
    monkeypatch.delenv("CATALYST_BATCH_SIZE", raising=False)
    return env


def _scan(**kw):
    kw.setdefault("sync", True)
    bt = kw.pop("background_tasks", None) or BackgroundTasks()
    return _run(gw.catalyst_alert_scan(bt, **kw))


def _item(sym, dec="BUY NOW", score=70, **kw):
    d = {"symbol": sym, "decision": dec, "score": score}
    d.update(kw)
    return d


# ═════════════════════════════════════════════════════════════════════════════
# GET /stockky-hot
# ═════════════════════════════════════════════════════════════════════════════

class TestStockkyHotEndpoint:
    def test_force_runs_the_full_scan_inline(self, cenv, kv):
        cenv.hot = {"universe_size": 9, "news_driven": [1]}
        out = _run(gw.stockky_hot_endpoint(force=True))
        assert out == {"universe_size": 9, "news_driven": [1]}
        call = cenv.hot_calls[0]
        assert call["force"] is True and call["max_symbols"] is None and call["progress_cb"] is None
        assert cenv.run_calls == []

    def test_cache_hit_is_served_marked_cached(self, cenv, kv):
        kv.store[gw.HOT_STOCKS_CACHE_KEY] = {"news_driven": ["x"], "cached": False}
        out = _run(gw.stockky_hot_endpoint(force=False, background_tasks=BackgroundTasks()))
        assert out == {"news_driven": ["x"], "cached": True}
        assert cenv.run_calls == [] and cenv.hot_calls == []

    def test_cold_cache_starts_the_background_job_and_returns_the_warming_placeholder(self, cenv, kv):
        bt = BackgroundTasks()
        out = _run(gw.stockky_hot_endpoint(force=False, background_tasks=bt))
        assert cenv.run_calls == [(bt, True)]
        assert out["ok"] is True and out["warming"] is True and "poll /stockky-hot/status" in out["message"]
        assert out["news_driven"] == out["results_driven"] == out["bulk_insider_driven"] == []
        assert cenv.hot_calls == []                                    # never blocks on a scan

    def test_cold_cache_with_a_stale_result_serves_it_flagged(self, cenv, kv):
        kv.store[gw.HOT_RESULT_KEY] = {"news_driven": ["old"], "generated_at": "then"}
        out = _run(gw.stockky_hot_endpoint(force=False, background_tasks=BackgroundTasks()))
        assert out == {"news_driven": ["old"], "generated_at": "then",
                       "cached": True, "stale": True, "warming": True}

    def test_failing_warm_up_start_is_logged_and_the_placeholder_still_returned(self, cenv, kv, caplog):
        cenv.run_raises = RuntimeError("cannot start")
        with caplog.at_level("WARNING"):
            out = _run(gw.stockky_hot_endpoint(force=False, background_tasks=BackgroundTasks()))
        assert out["warming"] is True and out["ok"] is True
        assert "failed to start" in caplog.text

    def test_without_background_tasks_no_job_is_started(self, cenv, kv):
        out = _run(gw.stockky_hot_endpoint(force=False))
        assert cenv.run_calls == [] and out["warming"] is True


# ═════════════════════════════════════════════════════════════════════════════
# GET /catalysts/alert/status
# ═════════════════════════════════════════════════════════════════════════════

class TestCatalystStatus:
    def test_no_job_yet(self, kv):
        assert gw.catalyst_alert_status() == {"ok": True}

    def test_non_dict_state_is_reported_idle(self, kv):
        kv.store[JOB_KEY] = "garbage"
        assert gw.catalyst_alert_status() == {"ok": True, "status": "idle"}

    def test_a_done_job_is_returned_as_is(self, kv):
        kv.store[JOB_KEY] = {"status": "done", "actionable_count": 3}
        assert gw.catalyst_alert_status() == {"ok": True, "status": "done", "actionable_count": 3}
        assert kv.sets == []

    def test_a_running_job_with_a_fresh_heartbeat_is_left_alone(self, kv):
        kv.store[JOB_KEY] = {"status": "running", "updated_at": _ago(30)}
        out = gw.catalyst_alert_status()
        assert out["status"] == "running" and kv.sets == []

    def test_a_running_job_silent_for_over_five_minutes_is_auto_failed_and_persisted(self, kv):
        kv.store[JOB_KEY] = {"status": "running", "updated_at": _ago(400), "processed": 7}
        out = gw.catalyst_alert_status()
        assert out["ok"] is True and out["status"] == "error" and out["processed"] == 7
        assert out["error"].startswith("stale_running age=40") and "worker likely slept" in out["error"]
        assert out["message"].startswith("Auto-failed: no progress for 40")
        key, saved, ttl = kv.sets[-1]
        assert key == JOB_KEY and saved["status"] == "error" and ttl == 86400

    def test_started_at_is_the_fallback_heartbeat(self, kv):
        kv.store[JOB_KEY] = {"status": "running", "started_at": _ago(900)}
        assert gw.catalyst_alert_status()["status"] == "error"

    def test_naive_timestamps_are_read_as_ist(self, kv):
        kv.store[JOB_KEY] = {"status": "running", "updated_at": _ago(400, naive=True)}
        assert gw.catalyst_alert_status()["status"] == "error"
        kv.store[JOB_KEY] = {"status": "running", "updated_at": _ago(20, naive=True)}
        assert gw.catalyst_alert_status()["status"] == "running"

    def test_a_z_suffixed_timestamp_is_parsed_as_utc(self, kv):
        # "…Z" means UTC; a stamp written as IST-wall-clock-with-Z is ~5.5h in the future of UTC reading,
        # i.e. a negative age, so it is never auto-failed.
        kv.store[JOB_KEY] = {"status": "running", "updated_at": _ago(0, z=True)}
        assert gw.catalyst_alert_status()["status"] == "running"

    def test_unparsable_or_missing_heartbeat_is_never_auto_failed(self, kv):
        kv.store[JOB_KEY] = {"status": "running", "updated_at": "not-a-date"}
        assert gw.catalyst_alert_status()["status"] == "running"
        kv.store[JOB_KEY] = {"status": "running"}
        assert gw.catalyst_alert_status()["status"] == "running"
        assert kv.sets == []

    def test_a_failing_heal_write_is_swallowed_but_the_healed_state_is_still_returned(self, kv):
        # Fixed (the code already did this; the pin was stale): the caller is still told "error", but the
        # response now carries heal_persisted=False so it is clear the stored job still says "running".
        kv.store[JOB_KEY] = {"status": "running", "updated_at": _ago(500)}
        kv.set_raises = True
        out = gw.catalyst_alert_status()
        assert out["status"] == "error" and out["heal_persisted"] is False
        assert kv.store[JOB_KEY]["status"] == "running"

    def test_a_successful_heal_carries_no_heal_persisted_flag(self, kv):
        kv.store[JOB_KEY] = {"status": "running", "updated_at": _ago(500)}
        out = gw.catalyst_alert_status()
        assert out["status"] == "error" and "heal_persisted" not in out
        assert kv.store[JOB_KEY]["status"] == "error"


# ═════════════════════════════════════════════════════════════════════════════
# GET|POST /catalysts/alert — parameters, guard and job record
# ═════════════════════════════════════════════════════════════════════════════

class TestCatalystScanSetup:
    @pytest.mark.parametrize("given,expected", [(0, 25), (3, 8), (8, 8), (30, 30), (100, 40)])
    def test_batch_size_is_clamped_to_8_40_with_a_default_of_25(self, cenv, given, expected):
        assert _scan(batch_size=given)["batch_size"] == expected
        assert cenv.hot_calls[-1]["env_batch"] == str(expected)

    def test_catalyst_batch_size_env_is_the_default(self, cenv, monkeypatch):
        monkeypatch.setenv("CATALYST_BATCH_SIZE", "12")
        assert _scan()["batch_size"] == 12

    def test_a_non_numeric_env_batch_size_falls_back_to_the_default(self, cenv, monkeypatch, kv):
        # FIXED: a bad CATALYST_BATCH_SIZE falls back to 25 instead of a 500.
        monkeypatch.setenv("CATALYST_BATCH_SIZE", "lots")
        _scan()
        assert kv.sets != []

    def test_background_mode_queues_the_work_and_answers_immediately(self, cenv, kv):
        bt = BackgroundTasks()
        out = _run(gw.catalyst_alert_scan(bt, batch_size=20))
        assert out["ok"] is True and out["status"] == "running" and out["accepted"] is True
        assert out["batch_size"] == 20 and out["poll"] == "/catalysts/alert/status"
        assert len(bt.tasks) == 1 and cenv.hot_calls == []
        job = kv.store[JOB_KEY]
        assert job["status"] == "running" and job["message"].startswith("Catalyst sweep started")
        assert job["force"] is True and job["notify"] is True and job["batch_size"] == 20
        assert job["processed"] == 0 and job["total"] == 0 and job["error"] is None and job["updated_at"]

    def test_the_queued_task_runs_the_same_work(self, cenv, kv):
        cenv.hot = {"universe_size": 5, "news_driven": [_item("AAA")]}
        bt = BackgroundTasks()
        _run(gw.catalyst_alert_scan(bt, notify=False))
        _run(bt())
        assert kv.store[JOB_KEY]["status"] == "done" and kv.store[JOB_KEY]["actionable_count"] == 1

    def test_force_and_notify_are_recorded_and_force_reaches_the_scan(self, cenv, kv):
        _scan(force=False, notify=False)
        job = kv.store[JOB_KEY]
        assert job["force"] is False and job["notify"] is False
        assert cenv.hot_calls[0]["force"] is False and cenv.hot_calls[0]["max_symbols"] is None

    def test_a_fresh_running_job_short_circuits_when_not_forced(self, cenv, kv):
        kv.store[JOB_KEY] = {"status": "running", "updated_at": _ago(60), "processed": 4}
        bt = BackgroundTasks()
        out = _run(gw.catalyst_alert_scan(bt, force=False))
        assert out["already_running"] is True and out["ok"] is True and out["processed"] == 4
        assert bt.tasks == [] and kv.sets == []

    def test_the_running_guard_is_not_bypassed_by_the_default_force(self, cenv, kv):
        # FIXED: `force` only bypasses the hot-stocks cache; a plain call no longer starts a second sweep.
        kv.store[JOB_KEY] = {"status": "running", "updated_at": _ago(10)}
        bt = BackgroundTasks()
        out = _run(gw.catalyst_alert_scan(bt))
        assert out["status"] == "running" and out.get("already_running") is True and len(bt.tasks) == 0

    def test_restart_true_overrides_the_running_guard(self, cenv, kv):
        kv.store[JOB_KEY] = {"status": "running", "updated_at": _ago(10)}
        bt = BackgroundTasks()
        out = _run(gw.catalyst_alert_scan(bt, restart=True))
        assert "already_running" not in out and len(bt.tasks) == 1

    @pytest.mark.parametrize("existing", [
        {"status": "running", "updated_at": _ago(200)},               # stale (>=180s) -> restart
        {"status": "running", "updated_at": "not-a-date"},            # unparsable -> restart
        {"status": "done", "updated_at": _ago(5)},                    # not running
        "garbage",                                                    # not a dict
    ])
    def test_stale_or_unusable_existing_state_does_not_block_a_new_run(self, cenv, kv, existing):
        kv.store[JOB_KEY] = existing
        bt = BackgroundTasks()
        out = _run(gw.catalyst_alert_scan(bt, force=False))
        assert out["accepted"] is True and len(bt.tasks) == 1

    def test_naive_and_z_heartbeats_are_read_for_the_guard(self, cenv, kv):
        kv.store[JOB_KEY] = {"status": "running", "started_at": _ago(30, naive=True)}
        assert _run(gw.catalyst_alert_scan(BackgroundTasks(), force=False))["already_running"] is True


# ═════════════════════════════════════════════════════════════════════════════
# GET|POST /catalysts/alert — the sweep itself
# ═════════════════════════════════════════════════════════════════════════════

class TestCatalystSweep:
    def test_progress_callback_updates_the_job_with_the_batch_and_position(self, cenv, kv):
        cenv.progress_probe = (4, 60, "TCS", 1)
        _scan(batch_size=20)
        during = cenv.job_during_hot
        assert during["status"] == "running" and during["processed"] == 5 and during["total"] == 60
        assert during["batch_index"] == 1 and during["batch_size"] == 20
        assert during["message"] == "Batch 2: scoring 5/60 (TCS)"

    def test_progress_works_when_the_stored_job_is_not_a_dict(self, cenv, kv):
        cenv.progress_probe = (0, 3, "AAA", 0)
        cenv.corrupt_job = True
        _scan()
        # _set_job resets a non-dict record rather than crashing; the callback's fields land on a fresh dict
        assert cenv.job_during_hot["status"] == "running" and cenv.job_during_hot["processed"] == 1
        assert kv.store[JOB_KEY]["status"] == "done"

    def test_only_actionable_or_high_signal_items_are_picked_and_ranked_by_section_then_score(self, cenv):
        cenv.hot = {
            "universe_size": 200,
            "news_driven": [_item("N1", score=90), _item("N2", "HOLD", 99), _item("N3", "DO NOT BUY", 50, signal_strength="high")],
            "results_driven": [_item("R1", "PREPARE TO BUY", 60), _item("R2", "buy now", 80)],
            "bulk_insider_driven": [_item("B1", "BUY NOW", 55)],
        }
        out = _scan(notify=False)
        assert [p["symbol"] for p in out["picks"]] == ["B1", "R2", "R1", "N1"]
        assert [p["section"] for p in out["picks"]] == ["bulk_insider_driven"] + ["results_driven"] * 2 + ["news_driven"]
        assert out["actionable_count"] == 4 and out["hot_universe_size"] == 200

    def test_a_high_signal_do_not_buy_row_is_not_listed_as_actionable(self, cenv):
        # FIXED: a "high" signal no longer lists a DO NOT BUY / SELL row.
        cenv.hot = {"universe_size": 1, "news_driven": [_item("ZZZ", "DO NOT BUY", 40, signal_strength="high")]}
        out = _scan(notify=False)
        assert out["actionable_count"] == 0 and out["picks"] == []

    def test_duplicate_symbols_keep_only_the_best_ranked_entry(self, cenv):
        cenv.hot = {"universe_size": 3,
                    "news_driven": [_item("abc", score=99)],
                    "bulk_insider_driven": [_item("ABC", score=10)],
                    "results_driven": [_item("", score=70), {"decision": "BUY NOW"}]}        # blank symbols dropped
        out = _scan(notify=False)
        assert len(out["picks"]) == 1 and out["picks"][0]["section"] == "bulk_insider_driven"

    def test_missing_scores_sort_as_zero(self, cenv):
        cenv.hot = {"universe_size": 2, "news_driven": [_item("LOW", score=None), _item("HIGH", score=5)]}
        assert [p["symbol"] for p in _scan(notify=False)["picks"]] == ["HIGH", "LOW"]

    def test_message_lists_at_most_15_names_and_the_result_keeps_20_picks(self, cenv):
        cenv.hot = {"universe_size": 99, "news_driven": [_item(f"S{i:02d}", score=100 - i) for i in range(25)]}
        out = _scan()
        assert out["actionable_count"] == 25 and len(out["picks"]) == 20
        assert len(out["message_preview"]) == 500                      # preview is cut at 500 chars
        msg = cenv.posts[0][1]["message"]
        assert "15. *S14*" in msg and "16." not in msg

    def test_message_format_reason_first_then_summary_fallback(self, cenv):
        cenv.hot = {"universe_size": 42, "news_driven": [
            _item("AAA", score=90, reasons=["x" * 120, "second"]),
            _item("BBB", score=80, reasons=[], summary="s" * 100),
            _item("CCC", "PREPARE TO BUY", 70, reasons="not-a-list", summary="fallback"),
            _item("DDD", score=60),
        ]}
        _scan()
        lines = cenv.posts[0][1]["message"].splitlines()
        assert lines[0] == "🔥 *Stockky Catalyst Alert*" and lines[1].startswith("IST ")
        assert lines[2] == "Universe screened: 42 · Actionable: 4" and lines[3] == ""
        assert lines[4] == "1. *AAA* — BUY NOW (score 90) [news]"
        assert lines[5] == f"   _{'x' * 80}_"
        assert lines[7] == f"   _{'s' * 80}_"
        assert lines[8] == "3. *CCC* — PREPARE TO BUY (score 70) [news]" and lines[9] == "   _fallback_"
        assert lines[10] == "4. *DDD* — BUY NOW (score 60) [news]" and len(lines) == 11

    def test_no_picks_message_and_no_notification(self, cenv):
        out = _scan()
        assert "No strong catalyst names right now." in out["message_preview"]
        assert out["actionable_count"] == 0 and out["notified"] is False and cenv.posts == []

    def test_notify_posts_the_message_to_the_notification_service(self, cenv):
        cenv.hot = {"universe_size": 7, "results_driven": [_item("AAA")]}
        out = _scan()
        assert out["notified"] is True and len(cenv.posts) == 1
        url, body = cenv.posts[0]
        assert url == "http://notif.local/notification/notify"
        assert body["title"] == "Catalyst Alert" and body["channel"] == "telegram"
        assert "*AAA* — BUY NOW" in body["message"] and cenv.telegram_calls == []

    def test_notify_false_skips_the_post(self, cenv):
        cenv.hot = {"universe_size": 7, "results_driven": [_item("AAA")]}
        assert _scan(notify=False)["notified"] is False and cenv.posts == []

    def test_a_non_2xx_notify_response_takes_the_telegram_fallback(self, cenv):
        # FIXED: a non-2xx reply is a failed delivery, so the fallback path runs.
        cenv.hot = {"universe_size": 7, "results_driven": [_item("AAA")]}
        cenv.post_status = 500
        out = _scan()
        assert len(cenv.telegram_calls) == 1 and out["notified"] is True

    def test_a_failing_post_falls_back_to_the_direct_telegram_sender(self, cenv, caplog):
        cenv.hot = {"universe_size": 7, "results_driven": [_item(f"S{i}", score=90 - i) for i in range(12)]}
        cenv.post_raises = RuntimeError("notif down")
        with caplog.at_level("WARNING"):
            out = _scan()
        assert out["notified"] is True and "catalyst telegram failed" in caplog.text
        assert len(cenv.telegram_calls) == 1
        recs = cenv.telegram_calls[0]["recommendations"]
        assert len(recs) == 10 and recs[0]["symbol"] == "S0"

    def test_a_failing_fallback_leaves_notified_false(self, cenv, caplog):
        cenv.hot = {"universe_size": 7, "results_driven": [_item("AAA")]}
        cenv.post_raises = RuntimeError("notif down")
        cenv.telegram_raises = RuntimeError("telegram down")
        with caplog.at_level("WARNING"):
            out = _scan()
        assert out["ok"] is True and out["notified"] is False and "catalyst telegram fallback" in caplog.text

    def test_missing_universe_size_prints_zero_and_reports_zero(self, cenv):
        # FIXED: the message line no longer prints "None".
        cenv.hot = {"news_driven": [_item("AAA")]}
        out = _scan(notify=False)
        assert "Universe screened: 0 · Actionable: 1" in out["message_preview"] and out["hot_universe_size"] == 0

    def test_a_none_scan_result_is_treated_as_empty(self, cenv):
        cenv.hot = None
        out = _scan()
        assert out["ok"] is True and out["actionable_count"] == 0 and out["hot_universe_size"] == 0

    def test_result_shape_and_the_stored_job_record(self, cenv, kv):
        cenv.hot = {"universe_size": 77, "news_driven": [_item("AAA")]}
        out = _scan(notify=False, batch_size=16)
        assert out["ok"] is True and out["status"] == "done" and out["batch_size"] == 16
        assert out["processed"] == out["total"] == 77
        assert out["message"] == "Done — screened 77 · 1 actionable (batched 16)"
        assert out["generated_at"]
        job = kv.store[JOB_KEY]
        assert job["status"] == "done" and job["actionable_count"] == 1 and job["notify"] is False
        assert job["picks"][0]["symbol"] == "AAA" and job["updated_at"]

    def test_keep_alive_loop_warms_upstreams_and_survives_timeouts_and_failures(self, cenv):
        cenv.hot_yields = 6
        cenv.keepalive_timeouts = 1          # first 75s wait "times out" -> the loop goes round again
        cenv.warm_raises = True              # and a failing warm never stops it
        out = _scan(notify=False)
        assert out["ok"] is True and cenv.warm_calls >= 2

    def test_a_keep_alive_task_that_will_not_stop_is_cancelled(self, cenv):
        cenv.hot_yields = 3
        cenv.final_wait_times_out = True
        assert _scan(notify=False)["status"] == "done"

    def test_scan_failure_marks_the_job_errored_and_returns_a_failure_envelope(self, cenv, kv):
        cenv.hot_raises = RuntimeError("e" * 400)
        out = _scan()
        assert out["ok"] is False and out["status"] == "error" and out["error"] == "e" * 300
        job = kv.store[JOB_KEY]
        assert job["status"] == "error" and job["error"] == "e" * 300 and job["message"].startswith("Error: ")
        assert cenv.posts == []

    def test_the_keep_alive_task_is_stopped_after_a_failed_scan(self):
        # FIXED: the keep-alive loop is stopped on every exit path (a `finally`).
        env = CEnv()
        env.hot_raises = RuntimeError("boom")
        env.hot_yields = 2
        k = KV()

        async def hot(force=False, max_symbols=None, progress_cb=None):
            for _ in range(env.hot_yields):
                await asyncio.sleep(0)
            raise env.hot_raises

        async def warm(client=None):
            env.warm_calls += 1

        mp = pytest.MonkeyPatch()
        try:
            mp.setattr(gw, "_redis_get", k.get)
            mp.setattr(gw, "_redis_set", k.set)
            mp.setattr(gw, "stockky_hot_stocks", hot)
            mp.setattr(gw, "_warm_upstream_services", warm)
            mp.setenv("HOT_BATCH_SIZE", "orig")

            async def go():
                out = await gw.catalyst_alert_scan(BackgroundTasks(), sync=True)
                leaked = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
                for t in leaked:
                    t.cancel()
                return out, len(leaked)

            out, leaked = asyncio.run(go())
        finally:
            mp.undo()
        assert out["ok"] is False and leaked == 0


# ═════════════════════════════════════════════════════════════════════════════
# ConnectionManager / ws_manager
# ═════════════════════════════════════════════════════════════════════════════

class FakeWS:
    def __init__(self, fail=False):
        self.accepted = False
        self.sent = []
        self.fail = fail

    async def accept(self):
        self.accepted = True

    async def send_text(self, msg):
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append(msg)


@pytest.fixture
def mgr():
    return gw.ConnectionManager()


class TestConnectionManager:
    def test_connect_accepts_and_registers_the_socket(self, mgr):
        ws = FakeWS()
        _run(mgr.connect(ws))
        assert ws.accepted and mgr.active == [ws]
        assert mgr.subs[id(ws)] == set() and mgr.quote_syms[id(ws)] == set()

    def test_disconnect_forgets_everything_and_is_idempotent(self, mgr):
        ws = FakeWS()
        _run(mgr.connect(ws))
        mgr.watch_quotes(ws, ["TCS"])
        mgr.disconnect(ws)
        assert mgr.active == [] and id(ws) not in mgr.subs and id(ws) not in mgr.quote_syms
        mgr.disconnect(ws)
        mgr.disconnect(FakeWS())

    def test_subscribe_and_unsubscribe(self, mgr):
        ws = FakeWS()
        _run(mgr.connect(ws))
        mgr.subscribe(ws, "scan")
        mgr.subscribe(ws, "jobs")
        mgr.unsubscribe(ws, "scan")
        mgr.unsubscribe(ws, "never-subscribed")
        assert mgr.subs[id(ws)] == {"jobs"}

    def test_subscribing_an_unknown_socket_creates_its_entry(self, mgr):
        ws = FakeWS()
        mgr.subscribe(ws, "scan")
        mgr.unsubscribe(FakeWS(), "scan")
        assert mgr.subs[id(ws)] == {"scan"}

    def test_watch_quotes_normalises_symbols_and_subscribes_each_channel(self, mgr):
        ws = FakeWS()
        _run(mgr.connect(ws))
        mgr.watch_quotes(ws, ["tcs.ns", " Infy.BO ", "RELIANCE", "", None, "  ", "tcs"])
        assert mgr.quote_syms[id(ws)] == {"TCS", "INFY", "RELIANCE"}
        assert mgr.subs[id(ws)] == {"quote:TCS", "quote:INFY", "quote:RELIANCE"}

    def test_unwatch_specific_symbols(self, mgr):
        ws = FakeWS()
        _run(mgr.connect(ws))
        mgr.watch_quotes(ws, ["TCS", "INFY", "SBIN"])
        mgr.unwatch_quotes(ws, ["tcs.NS", "NOTWATCHED", ""])
        assert mgr.quote_syms[id(ws)] == {"INFY", "SBIN"}
        assert mgr.subs[id(ws)] == {"quote:INFY", "quote:SBIN"}

    def test_unwatch_with_no_symbols_clears_the_whole_watch_list(self, mgr):
        ws = FakeWS()
        _run(mgr.connect(ws))
        mgr.subscribe(ws, "scan")
        mgr.watch_quotes(ws, ["TCS", "INFY"])
        mgr.unwatch_quotes(ws)
        assert mgr.quote_syms[id(ws)] == set() and mgr.subs[id(ws)] == {"scan"}

    def test_all_watched_symbols_is_the_sorted_union(self, mgr):
        a, b = FakeWS(), FakeWS()
        _run(mgr.connect(a))
        _run(mgr.connect(b))
        mgr.watch_quotes(a, ["TCS", "INFY"])
        mgr.watch_quotes(b, ["INFY", "SBIN"])
        assert mgr.all_watched_symbols() == ["INFY", "SBIN", "TCS"]
        assert gw.ConnectionManager().all_watched_symbols() == []

    def test_broadcast_reaches_only_subscribers_of_the_channel_or_all(self, mgr):
        a, b, c = FakeWS(), FakeWS(), FakeWS()
        for ws in (a, b, c):
            _run(mgr.connect(ws))
        mgr.subscribe(a, "scan")
        mgr.subscribe(b, "all")
        mgr.subscribe(c, "jobs")
        _run(mgr.broadcast("scan", {"pct": 50, "at": datetime(2026, 10, 1, 9, 30)}))
        assert len(a.sent) == 1 and len(b.sent) == 1 and c.sent == []
        msg = json.loads(a.sent[0])
        assert msg["channel"] == "scan" and msg["pct"] == 50 and msg["at"].startswith("2026-10-01")

    def test_broadcast_prunes_sockets_that_fail_to_send(self, mgr):
        good, dead = FakeWS(), FakeWS(fail=True)
        for ws in (good, dead):
            _run(mgr.connect(ws))
            mgr.subscribe(ws, "scan")
        _run(mgr.broadcast("scan", {"x": 1}))
        assert mgr.active == [good] and id(dead) not in mgr.subs and len(good.sent) == 1

    def test_a_payload_channel_key_cannot_override_the_channel_argument(self, mgr):
        # FIXED: `{**payload, "channel": channel}` — the channel argument wins.
        ws = FakeWS()
        _run(mgr.connect(ws))
        mgr.subscribe(ws, "scan")
        _run(mgr.broadcast("scan", {"channel": "other"}))
        assert json.loads(ws.sent[0])["channel"] == "scan"

    def test_the_module_level_manager_is_a_connection_manager(self):
        assert isinstance(gw.ws_manager, gw.ConnectionManager)


# ═════════════════════════════════════════════════════════════════════════════
# _resolve_quote_price
# ═════════════════════════════════════════════════════════════════════════════

class _MDResp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class QEnv:
    def __init__(self):
        self.md = _MDResp(200, {"price": 101.5})
        self.md_raises = None
        self.urls = []
        self.ticker = "TCS.NS"
        self.ticker_raises = None
        self.fast_info = {"last_price": 3500.0}
        self.has_fast_info = True
        self.yf_raises = None
        self.yf_calls = []


@pytest.fixture
def qenv(monkeypatch):
    env = QEnv()

    def get(url, timeout=None):
        env.urls.append((url, timeout))
        if env.md_raises:
            raise env.md_raises
        return env.md

    class Ticker:
        def __init__(self, sym):
            env.yf_calls.append(sym)
            if env.yf_raises:
                raise env.yf_raises
            if env.has_fast_info:
                self.fast_info = env.fast_info

    def resolve(sym):
        if env.ticker_raises:
            raise env.ticker_raises
        return env.ticker

    monkeypatch.setattr(gw.httpx, "get", get)
    monkeypatch.setattr(gw.yf, "Ticker", Ticker)
    monkeypatch.setattr(gw, "resolve_ns_ticker", resolve)
    monkeypatch.setattr(gw, "MARKET_DATA_URL", "http://md.local")
    return env


class TestResolveQuotePrice:
    def test_market_data_hit(self, qenv):
        qenv.md = _MDResp(200, {"price": "101.5", "close": 100, "change_pct": 1.5,
                                "as_of": "2026-10-01T10:00:00+05:30", "source": "nse"})
        out = gw._resolve_quote_price("TCS")
        assert out == {"symbol": "TCS", "price": 101.5, "close": 100, "change_pct": 1.5,
                       "as_of": "2026-10-01T10:00:00+05:30", "source": "nse"}
        assert qenv.urls == [("http://md.local/quote/TCS", 6)] and qenv.yf_calls == []

    def test_market_data_defaults(self, qenv):
        out = gw._resolve_quote_price("TCS")
        assert out["price"] == 101.5 and out["close"] == 101.5 and out["change_pct"] is None
        assert out["source"] == "market-data" and out["as_of"]

    @pytest.mark.parametrize("data,expected", [
        ({"regularMarketPrice": 7}, 7.0),
        ({"close": 8}, 8.0),
        ({"last": 9}, 9.0),
        ({"price": 0, "regularMarketPrice": 5}, 5.0),          # a zero price is treated as missing
    ])
    def test_price_key_fallbacks(self, qenv, data, expected):
        qenv.md = _MDResp(200, data)
        assert gw._resolve_quote_price("TCS")["price"] == expected

    @pytest.mark.parametrize("md", [
        _MDResp(503, {"price": 1}),                            # not a 200
        _MDResp(200, {}),                                      # no price at all
        _MDResp(200, {"price": "n/a"}),                        # float() fails
        _MDResp(200, ValueError("bad json")),                  # body is not JSON
    ])
    def test_unusable_market_data_falls_through_to_yfinance(self, qenv, md):
        qenv.md = md
        out = gw._resolve_quote_price("TCS")
        assert out["source"] == "yfinance_fast" and out["price"] == 3500.0 and qenv.yf_calls == ["TCS.NS"]

    def test_market_data_exception_falls_through_to_yfinance(self, qenv):
        qenv.md_raises = RuntimeError("timeout")
        assert gw._resolve_quote_price("TCS")["source"] == "yfinance_fast"

    def test_yfinance_result_shape(self, qenv):
        qenv.md_raises = RuntimeError("down")
        out = gw._resolve_quote_price("TCS")
        assert set(out) == {"symbol", "price", "close", "as_of", "source"}
        assert out["close"] == out["price"] == 3500.0 and out["symbol"] == "TCS"

    def test_yfinance_camel_case_last_price_key(self, qenv):
        qenv.md_raises = RuntimeError("down")
        qenv.fast_info = {"lastPrice": 12.5}
        assert gw._resolve_quote_price("TCS")["price"] == 12.5

    @pytest.mark.parametrize("fast_info", [{}, {"last_price": 0}, {"last_price": "x"}, {"last_price": None}])
    def test_no_usable_yfinance_price_gives_none(self, qenv, fast_info):
        qenv.md_raises = RuntimeError("down")
        qenv.fast_info = fast_info
        assert gw._resolve_quote_price("TCS") is None

    def test_missing_fast_info_attribute_gives_none(self, qenv):
        qenv.md_raises = RuntimeError("down")
        qenv.has_fast_info = False
        assert gw._resolve_quote_price("TCS") is None

    def test_an_unresolvable_symbol_gives_none_without_calling_yfinance(self, qenv):
        qenv.md_raises = RuntimeError("down")
        qenv.ticker = None
        assert gw._resolve_quote_price("ZZZ") is None and qenv.yf_calls == []

    @pytest.mark.parametrize("attr", ["ticker_raises", "yf_raises"])
    def test_yfinance_side_errors_give_none(self, qenv, attr):
        qenv.md_raises = RuntimeError("down")
        setattr(qenv, attr, RuntimeError("yf down"))
        assert gw._resolve_quote_price("TCS") is None


class TestPass82StatusGuard:
    def test_an_unreadable_job_record_is_returned_as_is(self, monkeypatch):
        class BadDict(dict):
            def get(self, *a, **k):
                raise RuntimeError("corrupt")

        monkeypatch.setattr(gw, "_redis_get", lambda key: BadDict(status="running"))
        out = gw.catalyst_alert_status()
        assert out["ok"] is True and out["status"] == "running"
