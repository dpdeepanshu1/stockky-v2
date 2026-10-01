"""tests/test_main_ws_loops_startup.py — coverage for api-gateway/main.py, slice 14 (lines 8629-9054)

Pass 72. The realtime half of the gateway and its startup hooks:

* `_quote_broadcast_loop` — the four idle gates (paused / scan running / loop disabled, no clients, market
  closed, no watched symbols), the 12-symbol cap, per-quote broadcast + metric, the price-alert pass (5 per
  tick, per-alert failure isolation, `evaluate_price_alerts` failure) and the 8s / 20s cadence;
* `_ensure_quote_loop` / `_ensure_jobs_loop` — task creation, reuse, re-creation once done, no-loop failure;
* `websocket_endpoint` (`/ws`) — greeting, ping, `subscribe_quotes` / `unsubscribe_quotes`, `subscribe` /
  `unsubscribe` for plain, `quote:` and `scan:` channels, `poll_scan`, malformed frames, disconnect and
  error handling;
* `_ws_push_scan`;
* `_collect_job_snapshots`, `_job_is_active`, `_jobs_broadcast_loop`;
* the three `@app.on_event("startup")` hooks (`startup_event`, `_warm_surprise_scan_cache`,
  `_warm_momentum_movers_cache`).

The endless loops are driven by replacing `asyncio` inside `main` with a proxy whose `sleep` records the delay
and raises a private `BaseException` after N calls, so each test runs an exact number of iterations. The
WebSocket is a scripted fake (`receive_text` pops a queue, then raises `WebSocketDisconnect`), the hub is a
fake or a fresh `ConnectionManager`, and every upstream (quotes, price alerts, job sources, market-data
status, indices, surprise engine) is faked. Nothing touches the network, a database or a real socket.
Findings are pinned as current behaviour and marked ``NOT FIXED``.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_ws_loops_startup.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import types

import pytest
from fastapi import WebSocketDisconnect

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)

import data_feed
import ipo_scanner
import refill_additional
import surprise_premarket
import surprise_scanner


def _run(coro):
    return asyncio.run(coro)


class _Stop(BaseException):
    """Raised from the fake `asyncio.sleep` to break out of a `while True` loop."""


# ═════════════════════════════════════════════════════════════════════════════
# Shared fakes
# ═════════════════════════════════════════════════════════════════════════════

class LoopEnv:
    def __init__(self):
        self.sleeps = []
        self.limit = 1                 # stop on the Nth sleep (recorded first)
        self.no_loop = False
        self.wait_timeout = False
        self.wait_timeouts = []
        self.create_task_raises = False


class _AioProxy:
    """Stands in for `asyncio` inside main."""

    def __init__(self, env):
        self._env = env

    async def sleep(self, secs):
        self._env.sleeps.append(secs)
        if len(self._env.sleeps) >= self._env.limit:
            raise _Stop()

    def get_event_loop(self):
        if self._env.no_loop:
            raise RuntimeError("no running loop")
        return asyncio.get_event_loop()

    async def wait_for(self, aw, timeout=None):
        self._env.wait_timeouts.append(timeout)
        if self._env.wait_timeout:
            aw.close()
            raise asyncio.TimeoutError()
        return await asyncio.wait_for(aw, timeout=timeout)

    def create_task(self, coro):
        if self._env.create_task_raises:
            coro.close()
            raise RuntimeError("cannot schedule")
        return asyncio.create_task(coro)

    def __getattr__(self, name):
        return getattr(asyncio, name)


@pytest.fixture
def lenv(monkeypatch):
    env = LoopEnv()
    monkeypatch.setattr(gw, "asyncio", _AioProxy(env))
    return env


def _drive(coro_fn, env, limit):
    env.sleeps.clear()
    env.limit = limit
    with pytest.raises(_Stop):
        _run(coro_fn())
    return list(env.sleeps)


class KV:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)


@pytest.fixture
def kv(monkeypatch):
    k = KV()
    monkeypatch.setattr(gw, "_redis_get", k.get)
    return k


class FakeHub:
    """Minimal stand-in for `ws_manager` used by the loops."""

    def __init__(self):
        self.active = [object()]
        self.symbols = []
        self.broadcasts = []
        self.broadcast_raises = None

    def all_watched_symbols(self):
        return list(self.symbols)

    async def broadcast(self, channel, payload):
        if self.broadcast_raises:
            raise self.broadcast_raises
        self.broadcasts.append((channel, payload))


# ═════════════════════════════════════════════════════════════════════════════
# _quote_broadcast_loop
# ═════════════════════════════════════════════════════════════════════════════

class QEnv:
    def __init__(self):
        self.hub = FakeHub()
        self.phase = "open"
        self.quotes = {}
        self.resolve_calls = []
        self.resolve_raises = None
        self.alerts = []
        self.alerts_raises = None
        self.alert_calls = 0
        self.wake_calls = 0
        self.wake_raises = False
        self.posts = []
        self.post_raises_for = set()
        self.metric_incs = []
        self.metric_raises = False


@pytest.fixture
def qenv(monkeypatch):
    env = QEnv()

    def resolve(sym):
        env.resolve_calls.append(sym)
        if env.resolve_raises:
            raise env.resolve_raises
        return env.quotes.get(sym)

    def evaluate():
        env.alert_calls += 1
        if env.alerts_raises:
            raise env.alerts_raises
        return list(env.alerts)

    def wake():
        env.wake_calls += 1
        if env.wake_raises:
            raise RuntimeError("wake failed")
        return True

    def post(url, json=None, timeout=None):
        env.posts.append((url, json, timeout))
        if json and json["title"].split("· ")[-1] in env.post_raises_for:
            raise RuntimeError("notify down")

    class Metrics:
        def inc(self, name, *a, **k):
            if env.metric_raises:
                raise RuntimeError("metrics down")
            env.metric_incs.append(name)

    monkeypatch.setattr(gw, "ws_manager", env.hub)
    monkeypatch.setattr(gw, "activity_paused", lambda: False)
    monkeypatch.setattr(gw, "scan_in_progress", lambda: False)
    monkeypatch.setattr(gw, "_QUOTE_LOOP_ENABLED", True)
    monkeypatch.setattr(gw, "_market_session_phase_ist", lambda: env.phase)
    monkeypatch.setattr(gw, "_resolve_quote_price", resolve)
    monkeypatch.setattr(gw, "_wake_notification_service", wake)
    monkeypatch.setattr(gw, "NOTIFICATION_URL", "http://notif.local")
    monkeypatch.setattr(gw, "metrics", Metrics())
    monkeypatch.setattr(gw.httpx, "post", post)
    monkeypatch.setattr(data_feed, "evaluate_price_alerts", evaluate)
    return env


def _alert(sym="TCS", **kw):
    d = {"symbol": sym, "current_price": 4000, "direction": "above", "target_price": 3900,
         "note": "n", "id": f"id-{sym}"}
    d.update(kw)
    return d


class TestQuoteLoopGates:
    @pytest.mark.parametrize("which", ["paused", "scan", "disabled"])
    def test_pause_scan_or_disabled_loop_only_sleeps_30s(self, lenv, qenv, monkeypatch, which):
        if which == "paused":
            monkeypatch.setattr(gw, "activity_paused", lambda: True)
        elif which == "scan":
            monkeypatch.setattr(gw, "scan_in_progress", lambda: True)
        else:
            monkeypatch.setattr(gw, "_QUOTE_LOOP_ENABLED", False)
        qenv.hub.symbols = ["TCS"]
        assert _drive(gw._quote_broadcast_loop, lenv, 2) == [30, 30]
        assert qenv.resolve_calls == [] and qenv.alert_calls == 0

    def test_no_connected_clients_sleeps_20s_without_any_upstream_call(self, lenv, qenv):
        qenv.hub.active = []
        qenv.hub.symbols = ["TCS"]
        assert _drive(gw._quote_broadcast_loop, lenv, 1) == [20]
        assert qenv.resolve_calls == [] and qenv.alert_calls == 0

    def test_a_hub_without_an_active_list_counts_as_no_clients(self, lenv, qenv):
        qenv.hub.active = None
        assert _drive(gw._quote_broadcast_loop, lenv, 1) == [20]

    @pytest.mark.parametrize("phase", ["closed", "weekend", "holiday", ""])
    def test_outside_the_live_session_sleeps_90s(self, lenv, qenv, phase):
        qenv.phase = phase
        qenv.hub.symbols = ["TCS"]
        assert _drive(gw._quote_broadcast_loop, lenv, 1) == [90]
        assert qenv.resolve_calls == []

    def test_no_watched_symbols_sleeps_20s(self, lenv, qenv):
        assert _drive(gw._quote_broadcast_loop, lenv, 1) == [20]
        assert qenv.resolve_calls == []

    def test_price_alerts_are_not_evaluated_on_any_idle_iteration(self, lenv, qenv, monkeypatch):
        # NOT FIXED: every idle gate uses `continue`, which skips the price-alert pass below the try block.
        # Alerts are only evaluated on an iteration that has clients, a live session AND watched symbols,
        # so with the market closed or nobody watching a quote, a triggered rule is never noticed here.
        qenv.alerts = [_alert()]
        qenv.phase = "closed"
        _drive(gw._quote_broadcast_loop, lenv, 3)
        qenv.hub.active = []
        _drive(gw._quote_broadcast_loop, lenv, 3)
        qenv.hub.active = [object()]
        qenv.phase = "open"
        _drive(gw._quote_broadcast_loop, lenv, 3)           # no symbols
        assert qenv.alert_calls == 0 and qenv.posts == []


class TestQuoteLoopBroadcast:
    def test_each_quote_is_broadcast_on_its_own_channel_and_counted(self, lenv, qenv):
        qenv.hub.symbols = ["TCS", "INFY"]
        qenv.quotes = {"TCS": {"symbol": "TCS", "price": 4000.0}, "INFY": {"symbol": "INFY", "price": 1500.0}}
        sleeps = _drive(gw._quote_broadcast_loop, lenv, 3)
        assert sleeps == [0.2, 0.2, 8]
        assert qenv.hub.broadcasts == [
            ("quote:TCS", {"type": "quote", "symbol": "TCS", "price": 4000.0}),
            ("quote:INFY", {"type": "quote", "symbol": "INFY", "price": 1500.0}),
        ]
        assert qenv.metric_incs == ["stockky_ws_quote_push_total"] * 2

    def test_symbols_without_a_quote_are_skipped_but_still_paced(self, lenv, qenv):
        qenv.hub.symbols = ["TCS", "NOPE"]
        qenv.quotes = {"TCS": {"symbol": "TCS", "price": 1.0}}
        assert _drive(gw._quote_broadcast_loop, lenv, 3) == [0.2, 0.2, 8]
        assert [c for c, _ in qenv.hub.broadcasts] == ["quote:TCS"]

    def test_only_the_first_twelve_symbols_are_fetched(self, lenv, qenv):
        qenv.hub.symbols = [f"S{i:02d}" for i in range(15)]
        _drive(gw._quote_broadcast_loop, lenv, 13)
        assert qenv.resolve_calls == [f"S{i:02d}" for i in range(12)]

    def test_a_failing_metric_does_not_stop_the_broadcast(self, lenv, qenv):
        qenv.hub.symbols = ["TCS"]
        qenv.quotes = {"TCS": {"symbol": "TCS", "price": 1.0}}
        qenv.metric_raises = True
        assert _drive(gw._quote_broadcast_loop, lenv, 2) == [0.2, 8]
        assert len(qenv.hub.broadcasts) == 1

    def test_an_error_while_fetching_is_swallowed_and_the_alert_pass_still_runs(self, lenv, qenv):
        qenv.hub.symbols = ["TCS"]
        qenv.resolve_raises = RuntimeError("quote down")
        qenv.alerts = [_alert()]
        assert _drive(gw._quote_broadcast_loop, lenv, 1) == [8]
        assert qenv.alert_calls == 1 and len(qenv.posts) == 1

    def test_a_failing_broadcast_aborts_the_rest_of_the_symbols_for_that_tick(self, lenv, qenv):
        # NOT FIXED: the per-symbol loop sits inside one try, so one failing broadcast skips the remaining
        # symbols until the next tick.
        qenv.hub.symbols = ["TCS", "INFY"]
        qenv.quotes = {"TCS": {"price": 1.0}, "INFY": {"price": 2.0}}
        qenv.hub.broadcast_raises = RuntimeError("hub down")
        _drive(gw._quote_broadcast_loop, lenv, 1)
        assert qenv.resolve_calls == ["TCS"]

    @pytest.mark.parametrize("phase,expected", [("open", 8), ("preopen", 20), ("post", 20)])
    def test_cadence_is_8s_when_open_and_20s_in_preopen_or_post(self, lenv, qenv, phase, expected):
        qenv.phase = phase
        qenv.hub.symbols = ["TCS"]
        assert _drive(gw._quote_broadcast_loop, lenv, 2) == [0.2, expected]


class TestQuoteLoopPriceAlerts:
    def test_a_triggered_alert_is_notified_and_broadcast(self, lenv, qenv):
        qenv.hub.symbols = ["TCS"]
        qenv.alerts = [_alert("TCS")]
        _drive(gw._quote_broadcast_loop, lenv, 2)
        assert qenv.wake_calls == 1
        url, body, timeout = qenv.posts[0]
        assert url == "http://notif.local/notify" and timeout == 8
        assert body == {"title": "Price Alert · TCS", "message": "⚡ TCS ₹4000 (above target ₹3900)",
                        "channel": "all"}
        channel, payload = qenv.hub.broadcasts[-1]
        assert channel == "alerts" and payload == {
            "type": "price_alert", "symbol": "TCS", "current_price": 4000, "target_price": 3900,
            "direction": "above", "note": "n", "id": "id-TCS"}

    def test_at_most_five_alerts_are_processed_per_tick(self, lenv, qenv):
        qenv.hub.symbols = ["TCS"]
        qenv.alerts = [_alert(f"S{i}") for i in range(8)]
        _drive(gw._quote_broadcast_loop, lenv, 2)
        assert [p[1]["title"] for p in qenv.posts] == [f"Price Alert · S{i}" for i in range(5)]

    def test_one_failing_alert_does_not_block_the_next(self, lenv, qenv):
        qenv.hub.symbols = ["TCS"]
        qenv.alerts = [_alert("AAA"), _alert("BBB")]
        qenv.post_raises_for = {"AAA"}
        _drive(gw._quote_broadcast_loop, lenv, 2)
        assert len(qenv.posts) == 2
        assert [c for c, p in qenv.hub.broadcasts if c == "alerts" and p["symbol"] == "BBB"] == ["alerts"]
        assert not [1 for c, p in qenv.hub.broadcasts if c == "alerts" and p["symbol"] == "AAA"]

    def test_a_failing_wake_skips_that_alert_entirely(self, lenv, qenv):
        qenv.hub.symbols = ["TCS"]
        qenv.alerts = [_alert()]
        qenv.wake_raises = True
        _drive(gw._quote_broadcast_loop, lenv, 2)
        assert qenv.posts == [] and qenv.hub.broadcasts == []

    def test_evaluate_price_alerts_failure_is_logged_and_the_loop_carries_on(self, lenv, qenv, caplog):
        qenv.hub.symbols = ["TCS"]
        qenv.alerts_raises = RuntimeError("db down")
        with caplog.at_level("DEBUG"):
            assert _drive(gw._quote_broadcast_loop, lenv, 2) == [0.2, 8]
        assert "quote loop alerts" in caplog.text

    def test_no_triggered_alerts_means_no_notification(self, lenv, qenv):
        qenv.hub.symbols = ["TCS"]
        _drive(gw._quote_broadcast_loop, lenv, 2)
        assert qenv.alert_calls == 1 and qenv.posts == [] and qenv.wake_calls == 0


# ═════════════════════════════════════════════════════════════════════════════
# _ensure_quote_loop / _ensure_jobs_loop
# ═════════════════════════════════════════════════════════════════════════════

class TestEnsureLoops:
    @pytest.mark.parametrize("ensure,task_attr,loop_attr", [
        ("_ensure_quote_loop", "_quote_loop_task", "_quote_broadcast_loop"),
        ("_ensure_jobs_loop", "_jobs_ws_task", "_jobs_broadcast_loop"),
    ])
    def test_task_is_created_reused_while_alive_and_recreated_once_done(
            self, monkeypatch, ensure, task_attr, loop_attr):
        started = []
        release = {}

        async def fake_loop():
            started.append(1)
            await release["ev"].wait()

        monkeypatch.setattr(gw, loop_attr, fake_loop)
        monkeypatch.setattr(gw, task_attr, None)

        async def scenario():
            release["ev"] = asyncio.Event()
            fn = getattr(gw, ensure)
            fn()
            first = getattr(gw, task_attr)
            await asyncio.sleep(0)
            fn()
            assert getattr(gw, task_attr) is first and started == [1]      # still running -> reused
            release["ev"].set()
            await first
            fn()
            second = getattr(gw, task_attr)
            assert second is not first
            await second
            return len(started)

        assert _run(scenario()) == 2

    @pytest.mark.parametrize("ensure,task_attr", [("_ensure_quote_loop", "_quote_loop_task"),
                                                  ("_ensure_jobs_loop", "_jobs_ws_task")])
    def test_failure_to_get_a_loop_is_swallowed(self, lenv, monkeypatch, caplog, ensure, task_attr):
        monkeypatch.setattr(gw, task_attr, None)
        lenv.no_loop = True
        with caplog.at_level("DEBUG"):
            getattr(gw, ensure)()
        assert getattr(gw, task_attr) is None and "loop start" in caplog.text


# ═════════════════════════════════════════════════════════════════════════════
# /ws
# ═════════════════════════════════════════════════════════════════════════════

class FakeSocket:
    def __init__(self, frames, end=None, send_fail_after=None):
        self.frames = list(frames)
        self.end = end if end is not None else WebSocketDisconnect()
        self.accepted = False
        self.sent = []
        self.send_fail_after = send_fail_after

    async def accept(self):
        self.accepted = True

    async def send_text(self, msg):
        if self.send_fail_after is not None and len(self.sent) >= self.send_fail_after:
            raise RuntimeError("send failed")
        self.sent.append(json.loads(msg))

    async def receive_text(self):
        if self.frames:
            f = self.frames.pop(0)
            return f if isinstance(f, str) else json.dumps(f)
        raise self.end

    def by_type(self, t):
        return [m for m in self.sent if m.get("type") == t]


class WEnv:
    def __init__(self):
        self.hub = gw.ConnectionManager()
        self.quotes = {}
        self.resolve_calls = []
        self.ensure_calls = {"quote": 0, "jobs": 0}


@pytest.fixture
def wenv(monkeypatch, kv):
    env = WEnv()

    def resolve(sym):
        env.resolve_calls.append(sym)
        return env.quotes.get(sym)

    monkeypatch.setattr(gw, "ws_manager", env.hub)
    monkeypatch.setattr(gw, "_resolve_quote_price", resolve)
    monkeypatch.setattr(gw, "_ensure_quote_loop", lambda: env.ensure_calls.__setitem__("quote", env.ensure_calls["quote"] + 1))
    monkeypatch.setattr(gw, "_ensure_jobs_loop", lambda: env.ensure_calls.__setitem__("jobs", env.ensure_calls["jobs"] + 1))
    return env


def _ws(frames, **kw):
    ws = FakeSocket(frames, **kw)
    return ws


def _serve(ws):
    _run(gw.websocket_endpoint(ws))
    return ws


class TestWebsocketBasics:
    def test_connect_greets_starts_the_loops_and_cleans_up_on_disconnect(self, wenv):
        ws = _serve(_ws([]))
        assert ws.accepted
        hello = ws.sent[0]
        assert hello["channel"] == "system" and hello["type"] == "connected" and hello["ts"]
        assert hello["features"] == ["scan", "quotes", "jobs", "ping"]
        assert wenv.ensure_calls == {"quote": 1, "jobs": 1}
        assert wenv.hub.active == [] and wenv.hub.subs == {}

    def test_ping_gets_a_pong_and_action_case_is_ignored(self, wenv):
        ws = _serve(_ws([{"action": "ping"}, {"action": "PING"}]))
        assert ws.by_type("pong") == [{"channel": "system", "type": "pong"}] * 2

    @pytest.mark.parametrize("frame", ["", "not json", "{broken", {"action": "nope"}, {}, {"action": "subscribe"},
                                       {"action": "unsubscribe"}, {"action": "poll_scan"}])
    def test_unknown_empty_or_incomplete_frames_are_silently_ignored(self, wenv, frame):
        ws = _serve(_ws([frame, {"action": "ping"}]))
        assert [m["type"] for m in ws.sent] == ["connected", "pong"]

    @pytest.mark.parametrize("frame", ["[1, 2]", "5", '"ping"', "null", '{"action": 123}',
                                       '{"action": "subscribe_quotes", "symbols": 5}'])
    def test_a_non_object_or_badly_typed_frame_closes_the_connection(self, wenv, frame, caplog):
        # NOT FIXED: only `json.loads` is guarded. A frame that parses to a non-dict, a non-string `action`
        # or a non-iterable `symbols` raises outside that try, lands in the catch-all and the socket is
        # dropped (and later frames are never read).
        with caplog.at_level("WARNING"):
            ws = _serve(_ws([frame, {"action": "ping"}]))
        assert [m["type"] for m in ws.sent] == ["connected"]
        assert "websocket closed" in caplog.text and wenv.hub.active == []

    def test_a_receive_error_is_logged_and_the_socket_removed(self, wenv, caplog):
        with caplog.at_level("WARNING"):
            ws = _serve(_ws([{"action": "ping"}], end=RuntimeError("network reset")))
        assert ws.by_type("pong") and "network reset" in caplog.text and wenv.hub.active == []

    def test_a_failing_greeting_is_handled_like_any_other_error(self, wenv, caplog):
        with caplog.at_level("WARNING"):
            ws = _serve(_ws([], send_fail_after=0))
        assert ws.sent == [] and "websocket closed" in caplog.text and wenv.hub.active == []


class TestWebsocketQuotes:
    def test_subscribe_quotes_acks_sorted_symbols_then_pushes_snapshots(self, wenv):
        wenv.quotes = {"TCS": {"symbol": "TCS", "price": 4000.0}, "INFY": {"symbol": "INFY", "price": 1500.0}}
        ws = _serve(_ws([{"action": "subscribe_quotes", "symbols": ["tcs.ns", "INFY", "NOQUOTE", ""]}]))
        ack = ws.by_type("quotes_subscribed")[0]
        assert ack == {"channel": "system", "type": "quotes_subscribed", "symbols": ["INFY", "NOQUOTE", "TCS"]}
        snaps = {m["channel"]: m for m in ws.by_type("quote")}
        assert set(snaps) == {"quote:TCS", "quote:INFY"} and snaps["quote:TCS"]["price"] == 4000.0
        assert sorted(wenv.resolve_calls) == ["INFY", "NOQUOTE", "TCS"]       # NOQUOTE resolved to None -> no push

    def test_a_single_string_symbol_is_accepted(self, wenv):
        ws = _serve(_ws([{"action": "subscribe_quotes", "symbols": "TCS"}]))
        assert ws.by_type("quotes_subscribed")[0]["symbols"] == ["TCS"]

    def test_missing_symbols_acks_an_empty_list(self, wenv):
        ws = _serve(_ws([{"action": "subscribe_quotes"}]))
        assert ws.by_type("quotes_subscribed")[0]["symbols"] == [] and wenv.resolve_calls == []

    def test_only_ten_immediate_snapshots_are_pushed(self, wenv):
        syms = [f"S{i:02d}" for i in range(13)]
        wenv.quotes = {s: {"symbol": s, "price": 1.0} for s in syms}
        ws = _serve(_ws([{"action": "subscribe_quotes", "symbols": syms}]))
        assert len(ws.by_type("quotes_subscribed")[0]["symbols"]) == 13
        assert len(ws.by_type("quote")) == 10 and len(wenv.resolve_calls) == 10

    def test_unsubscribe_quotes_acks_for_a_list_a_string_or_no_symbols(self, wenv):
        ws = _serve(_ws([
            {"action": "subscribe_quotes", "symbols": ["TCS", "INFY", "SBIN"]},
            {"action": "unsubscribe_quotes", "symbols": ["TCS"]},
            {"action": "unsubscribe_quotes", "symbols": "INFY"},
            {"action": "unsubscribe_quotes"},
        ]))
        assert ws.by_type("quotes_unsubscribed") == [{"channel": "system", "type": "quotes_unsubscribed"}] * 3

    def test_unsubscribing_specific_symbols_leaves_the_rest_watched(self, wenv, monkeypatch):
        left = []
        orig = wenv.hub.unwatch_quotes

        def spy(ws, symbols=None):
            orig(ws, symbols)
            left.append(set(wenv.hub.quote_syms[id(ws)]))

        monkeypatch.setattr(wenv.hub, "unwatch_quotes", spy)
        _serve(_ws([{"action": "subscribe_quotes", "symbols": ["TCS", "INFY", "SBIN"]},
                    {"action": "unsubscribe_quotes", "symbols": ["TCS"]},
                    {"action": "unsubscribe_quotes", "symbols": "INFY"}]))
        assert left == [{"INFY", "SBIN"}, {"SBIN"}]

    def test_unsubscribe_without_symbols_clears_the_watch_list_before_disconnect(self, wenv, monkeypatch):
        seen = {}
        orig = wenv.hub.unwatch_quotes

        def spy(ws, symbols=None):
            orig(ws, symbols)
            seen["left"] = set(wenv.hub.quote_syms[id(ws)])
            seen["symbols"] = symbols

        monkeypatch.setattr(wenv.hub, "unwatch_quotes", spy)
        _serve(_ws([{"action": "subscribe_quotes", "symbols": ["TCS"]}, {"action": "unsubscribe_quotes"}]))
        assert seen == {"left": set(), "symbols": None}


class TestWebsocketChannels:
    def test_plain_channel_subscribe_acks_only(self, wenv):
        seen = {}
        orig = wenv.hub.subscribe

        def spy(ws, ch):
            orig(ws, ch)
            seen.setdefault("subs", set()).update(wenv.hub.subs[id(ws)])

        wenv.hub.subscribe = spy
        ws = _serve(_ws([{"action": "subscribe", "channel": " jobs "}]))
        assert ws.by_type("subscribed") == [{"channel": "jobs", "type": "subscribed"}]
        assert seen["subs"] == {"jobs"} and len(ws.sent) == 2

    def test_quote_channel_subscribe_watches_the_symbol_and_pushes_a_snapshot(self, wenv):
        wenv.quotes = {"TCS": {"symbol": "TCS", "price": 4000.0}}
        seen = {}
        orig = wenv.hub.watch_quotes

        def spy(ws, symbols):
            orig(ws, symbols)
            seen["subs"] = set(wenv.hub.subs[id(ws)])
            seen["watched"] = set(wenv.hub.quote_syms[id(ws)])

        wenv.hub.watch_quotes = spy
        ws = _serve(_ws([{"action": "subscribe", "channel": "quote:TCS"}]))
        assert ws.by_type("subscribed")[0]["channel"] == "quote:TCS"
        assert ws.by_type("quote")[0] == {"channel": "quote:TCS", "type": "quote", "symbol": "TCS", "price": 4000.0}
        assert seen == {"subs": {"quote:TCS"}, "watched": {"TCS"}}

    def test_quote_channel_without_a_quote_only_acks(self, wenv):
        ws = _serve(_ws([{"action": "subscribe", "channel": "quote:ZZZ"}]))
        assert ws.by_type("quote") == [] and len(ws.by_type("subscribed")) == 1

    def test_lowercase_quote_channel_keeps_a_second_raw_subscription_and_raw_snapshot_symbol(self, wenv):
        # NOT FIXED: `subscribe(channel)` stores the channel as typed, `watch_quotes` normalises and
        # subscribes "quote:TCS" as well, and the snapshot resolves/answers with the raw text.
        wenv.quotes = {"tcs.ns": {"symbol": "tcs.ns", "price": 1.0}}
        seen = {}
        orig = wenv.hub.watch_quotes

        def spy(ws, symbols):
            orig(ws, symbols)
            seen["subs"] = set(wenv.hub.subs[id(ws)])

        wenv.hub.watch_quotes = spy
        ws = _serve(_ws([{"action": "subscribe", "channel": "quote:tcs.ns"}]))
        assert seen["subs"] == {"quote:tcs.ns", "quote:TCS"}
        assert wenv.resolve_calls == ["tcs.ns"] and ws.by_type("quote")[0]["channel"] == "quote:tcs.ns"

    @pytest.mark.parametrize("data,expected", [
        ({"status": "done", "processed": 5, "total": 5, "elapsed": 3.2, "result": {"n": 1}},
         {"status": "done", "processed": 5, "total": 5, "elapsed": 3.2, "result": {"n": 1}}),
        ({"status": "running", "processed": 2, "total": 9, "result": {"partial": True}},
         {"status": "running", "processed": 2, "total": 9, "elapsed": None, "result": None}),
        (None, {"status": None, "processed": 0, "total": 0, "elapsed": None, "result": None}),
    ])
    def test_scan_channel_subscribe_sends_the_task_status(self, wenv, kv, data, expected):
        if data is not None:
            kv.store[gw.SCAN_TASK_PREFIX + "t1"] = data
        ws = _serve(_ws([{"action": "subscribe", "channel": "scan:t1"}]))
        status = ws.by_type("scan_status")[0]
        assert status["channel"] == "scan:t1" and status["task_id"] == "t1"
        assert {k: status[k] for k in expected} == expected
        assert len(ws.by_type("subscribed")) == 1

    def test_unsubscribe_drops_the_channel_and_unwatches_quote_channels(self, wenv):
        seen = {}
        orig_unwatch = wenv.hub.unwatch_quotes

        def spy(ws, symbols=None):
            orig_unwatch(ws, symbols)
            seen["unwatch"] = symbols

        wenv.hub.unwatch_quotes = spy
        ws = _serve(_ws([
            {"action": "subscribe", "channel": "quote:TCS"},
            {"action": "subscribe", "channel": "jobs"},
            {"action": "unsubscribe", "channel": "jobs"},
            {"action": "unsubscribe", "channel": "quote:TCS"},
        ]))
        assert seen["unwatch"] == ["TCS"]
        assert len(ws.by_type("subscribed")) == 2 and ws.by_type("unsubscribed") == []     # no ack frame

    def test_poll_scan_replies_on_the_scan_channel(self, wenv, kv):
        kv.store[gw.SCAN_TASK_PREFIX + "t9"] = {"status": "done", "processed": 1, "total": 1, "result": [1]}
        ws = _serve(_ws([{"action": "poll_scan", "task_id": "t9"}, {"action": "poll_scan", "task_id": "none"}]))
        first, second = ws.by_type("scan_status")
        assert first["channel"] == "scan:t9" and first["result"] == [1]
        assert second["status"] is None and second["processed"] == 0 and second["result"] is None


# ═════════════════════════════════════════════════════════════════════════════
# _ws_push_scan
# ═════════════════════════════════════════════════════════════════════════════

class TestWsPushScan:
    @pytest.fixture
    def hub(self, monkeypatch):
        h = FakeHub()
        monkeypatch.setattr(gw, "ws_manager", h)
        return h

    @pytest.mark.parametrize("status,carried", [("done", True), ("cancelled", True), ("error", True),
                                                ("running", False), (None, False)])
    def test_result_is_only_pushed_for_terminal_states(self, hub, status, carried):
        _run(gw._ws_push_scan("t1", {"status": status, "processed": 4, "total": 8, "elapsed": 1.5, "result": {"k": 1}}))
        channel, payload = hub.broadcasts[0]
        assert channel == "scan:t1"
        assert payload == {"type": "scan_status", "task_id": "t1", "status": status, "processed": 4, "total": 8,
                           "elapsed": 1.5, "result": {"k": 1} if carried else None}

    def test_defaults_for_missing_fields(self, hub):
        _run(gw._ws_push_scan("t2", {}))
        assert hub.broadcasts[0][1] == {"type": "scan_status", "task_id": "t2", "status": None, "processed": 0,
                                        "total": 0, "elapsed": None, "result": None}

    def test_a_failing_broadcast_is_swallowed(self, hub, caplog):
        hub.broadcast_raises = RuntimeError("hub down")
        with caplog.at_level("DEBUG"):
            _run(gw._ws_push_scan("t3", {"status": "done"}))
        assert "ws push scan failed" in caplog.text


# ═════════════════════════════════════════════════════════════════════════════
# _collect_job_snapshots / _job_is_active / _jobs_broadcast_loop
# ═════════════════════════════════════════════════════════════════════════════

class JEnv:
    def __init__(self):
        self.sources = {"feed": {"status": "idle"}, "refill": {"status": "idle"},
                        "premarket": {"is_running": False}, "ipo": {"status": "idle"},
                        "rl": {"yfinance": {"queued": 0}}}
        self.raises = set()
        self.yahoo = (200, {"connected": True})
        self.yahoo_raises = False
        self.yahoo_calls = []


@pytest.fixture
def jenv(monkeypatch):
    env = JEnv()

    def src(name):
        def fn(*a, **k):
            if name in env.raises:
                raise RuntimeError(f"{name} down")
            return env.sources[name]
        return fn

    class Store:
        def job(self):
            return src("feed")()

    class Resp:
        def __init__(self, status, data):
            self.status_code, self._d = status, data

        def json(self):
            return self._d

    def get(url, timeout=None):
        env.yahoo_calls.append((url, timeout))
        if env.yahoo_raises:
            raise RuntimeError("yahoo ws down")
        return Resp(*env.yahoo)

    monkeypatch.setattr(data_feed, "get_data_feed_store", lambda: Store())
    monkeypatch.setattr(refill_additional, "get_refill_job", src("refill"))
    monkeypatch.setattr(surprise_premarket, "get_premarket_progress", src("premarket"))
    monkeypatch.setattr(ipo_scanner, "get_ipo_scan_progress", src("ipo"))
    monkeypatch.setattr(gw._rl, "stats", src("rl"))
    monkeypatch.setattr(gw.httpx, "get", get)
    monkeypatch.setattr(gw, "MARKET_DATA_URL", "http://md.local")
    return env


class TestCollectJobSnapshots:
    def test_everything_is_collected_under_its_own_key(self, jenv):
        out = gw._collect_job_snapshots()
        assert out == {"data_feed": {"status": "idle"}, "refill_additional": {"status": "idle"},
                       "surprise_premarket": {"is_running": False}, "ipo_scan": {"status": "idle"},
                       "rate_limits": {"yfinance": {"queued": 0}}, "yahoo_ws_feed": {"connected": True}}
        assert jenv.yahoo_calls == [("http://md.local/internal/yahoo-ws-status", 3)]

    @pytest.mark.parametrize("name,key", [("feed", "data_feed"), ("refill", "refill_additional"),
                                          ("premarket", "surprise_premarket"), ("ipo", "ipo_scan"),
                                          ("rl", "rate_limits")])
    def test_each_failing_source_is_dropped_without_affecting_the_others(self, jenv, name, key, caplog):
        jenv.raises = {name}
        with caplog.at_level("DEBUG"):
            out = gw._collect_job_snapshots()
        assert key not in out and len(out) == 5 and "jobs snapshot" in caplog.text

    def test_yahoo_status_needs_a_200_and_survives_errors(self, jenv):
        jenv.yahoo = (503, {"connected": False})
        assert "yahoo_ws_feed" not in gw._collect_job_snapshots()
        jenv.yahoo_raises = True
        assert "yahoo_ws_feed" not in gw._collect_job_snapshots()

    def test_all_sources_failing_gives_an_empty_snapshot(self, jenv):
        jenv.raises = {"feed", "refill", "premarket", "ipo", "rl"}
        jenv.yahoo_raises = True
        assert gw._collect_job_snapshots() == {}


class TestJobIsActive:
    @pytest.mark.parametrize("key", ["data_feed", "refill_additional", "surprise_premarket", "ipo_scan"])
    @pytest.mark.parametrize("status", ["running", "computing", "started"])
    def test_active_statuses(self, key, status):
        assert gw._job_is_active({key: {"status": status}}) is True

    @pytest.mark.parametrize("snap", [
        {}, {"data_feed": None}, {"data_feed": {"status": "idle"}}, {"data_feed": {"status": "done"}},
        {"data_feed": {}}, {"rate_limits": {"status": "running"}}, {"yahoo_ws_feed": {"status": "running"}},
        {"surprise_premarket": {"is_running": True}},
    ])
    def test_inactive_snapshots(self, snap):
        # NOT FIXED (last case): the premarket progress dict exposes `is_running`, not `status` — a
        # running premarket scan reporting only that flag never speeds the push interval up.
        assert gw._job_is_active(snap) is False


class TestJobsBroadcastLoop:
    @pytest.fixture
    def loop_env(self, monkeypatch, lenv):
        hub = FakeHub()
        monkeypatch.setattr(gw, "ws_manager", hub)
        snaps = {"value": {"data_feed": {"status": "idle"}}, "raises": None, "calls": 0}

        def collect():
            snaps["calls"] += 1
            if snaps["raises"]:
                raise snaps["raises"]
            return snaps["value"]

        monkeypatch.setattr(gw, "_collect_job_snapshots", collect)
        return hub, snaps

    def test_no_clients_sleeps_15s_without_collecting(self, loop_env, lenv):
        hub, snaps = loop_env
        hub.active = []
        assert _drive(gw._jobs_broadcast_loop, lenv, 2) == [15, 15] and snaps["calls"] == 0

    def test_idle_snapshot_is_broadcast_then_waits_15s(self, loop_env, lenv):
        hub, snaps = loop_env
        assert _drive(gw._jobs_broadcast_loop, lenv, 1) == [15]
        channel, payload = hub.broadcasts[0]
        assert channel == "jobs" and payload["type"] == "jobs_snapshot" and payload["ts"]
        assert payload["data_feed"] == {"status": "idle"}

    def test_an_active_job_speeds_the_loop_up_to_2s(self, loop_env, lenv):
        hub, snaps = loop_env
        snaps["value"] = {"refill_additional": {"status": "running"}}
        assert _drive(gw._jobs_broadcast_loop, lenv, 2) == [2, 2] and len(hub.broadcasts) == 2

    def test_a_collect_failure_is_logged_and_retried_after_15s(self, loop_env, lenv, caplog):
        hub, snaps = loop_env
        snaps["raises"] = RuntimeError("collect down")
        with caplog.at_level("DEBUG"):
            assert _drive(gw._jobs_broadcast_loop, lenv, 2) == [15, 15]
        assert "jobs broadcast loop" in caplog.text and hub.broadcasts == []

    def test_a_snapshot_cannot_override_type_or_ts(self, loop_env, lenv):
        # NOT FIXED: `**snap` is spread last, so a job source returning a "type"/"ts" key overrides them.
        hub, snaps = loop_env
        snaps["value"] = {"type": "x", "ts": "y"}
        _drive(gw._jobs_broadcast_loop, lenv, 1)
        assert hub.broadcasts[0][1]["type"] == "x" and hub.broadcasts[0][1]["ts"] == "y"


# ═════════════════════════════════════════════════════════════════════════════
# startup hooks
# ═════════════════════════════════════════════════════════════════════════════

class FakeRedis:
    def __init__(self, raises=False):
        self.deleted = []
        self.raises = raises

    def delete(self, key):
        if self.raises:
            raise RuntimeError("redis down")
        self.deleted.append(key)


class TestStartupEvent:
    @pytest.fixture
    def senv(self, monkeypatch, lenv):
        env = types.SimpleNamespace(redis=FakeRedis(), indices_calls=[], indices_raises=None)

        def indices(force_refresh=False):
            env.indices_calls.append(force_refresh)
            if env.indices_raises:
                raise env.indices_raises
            return {"ok": True}

        monkeypatch.setattr(gw, "_redis", env.redis)
        monkeypatch.setattr(gw, "get_market_indices", indices)
        return env

    def test_clears_the_old_indices_cache_and_force_warms(self, senv, lenv, caplog):
        with caplog.at_level("INFO"):
            _run(gw.startup_event())
        assert senv.redis.deleted == [gw.INDICES_CACHE_KEY] and senv.indices_calls == [True]
        assert "pre-populated successfully" in caplog.text
        assert lenv.wait_timeouts == [8.0]                  # hard 8s budget so a cold Yahoo cannot delay boot

    def test_a_failing_cache_clear_does_not_stop_the_warm(self, senv):
        senv.redis.raises = True
        _run(gw.startup_event())
        assert senv.indices_calls == [True]

    def test_redis_being_disabled_is_tolerated(self, senv, monkeypatch):
        monkeypatch.setattr(gw, "_redis", None)
        _run(gw.startup_event())
        assert senv.indices_calls == [True]

    def test_a_timeout_is_a_non_fatal_warning(self, senv, lenv, caplog):
        lenv.wait_timeout = True
        with caplog.at_level("WARNING"):
            _run(gw.startup_event())
        assert "timed out (non-fatal)" in caplog.text

    def test_a_warm_error_is_a_non_fatal_warning(self, senv, caplog):
        senv.indices_raises = RuntimeError("yahoo down")
        with caplog.at_level("WARNING"):
            _run(gw.startup_event())
        assert "indices warm failed (non-fatal): yahoo down" in caplog.text

    def test_the_outer_guard_catches_a_failing_log_call(self, senv, lenv, monkeypatch):
        # Defensive branch: the only thing that can raise outside the inner try blocks is the logging itself.
        lenv.wait_timeout = True
        calls = []

        class Log:
            def info(self, *a, **k):
                pass

            def debug(self, *a, **k):
                pass

            def warning(self, msg, *a, **k):
                calls.append(msg)
                if len(calls) == 1:
                    raise RuntimeError("log handler down")

        monkeypatch.setattr(gw, "logger", Log())
        _run(gw.startup_event())
        assert len(calls) == 2 and "indices, non-fatal" in calls[1]


def _drain():
    async def go():
        for _ in range(20):
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)
    return go


class TestWarmSurpriseScanCache:
    @pytest.fixture
    def env(self, monkeypatch, lenv):
        e = types.SimpleNamespace(scan_calls=[], scan_raises=None)

        class Engine:
            async def scan(self, client=None, market_data_url=None):
                e.scan_calls.append((client, market_data_url))
                if e.scan_raises:
                    raise e.scan_raises

        client = object()
        e.client = client
        monkeypatch.setattr(surprise_scanner, "surprise_engine", Engine())
        monkeypatch.setattr(gw, "_get_http_client", lambda: client)
        monkeypatch.setattr(gw, "MARKET_DATA_URL", "http://md.local")
        return e

    def test_the_first_scan_runs_in_the_background(self, env, caplog):
        async def go():
            await gw._warm_surprise_scan_cache()
            started_without_await = list(env.scan_calls)
            await _drain()()
            return started_without_await

        with caplog.at_level("INFO"):
            before = _run(go())
        assert before == [] and env.scan_calls == [(env.client, "http://md.local")]       # scheduled, not awaited
        assert "pre-warmed" in caplog.text

    def test_a_scan_failure_is_a_non_fatal_warning(self, env, caplog):
        env.scan_raises = RuntimeError("scan down")

        async def go():
            await gw._warm_surprise_scan_cache()
            await _drain()()

        with caplog.at_level("WARNING"):
            _run(go())
        assert "surprise-scan warm, non-fatal): scan down" in caplog.text

    def test_a_scheduling_failure_is_swallowed(self, env, lenv, caplog):
        lenv.create_task_raises = True
        with caplog.at_level("DEBUG"):
            _run(gw._warm_surprise_scan_cache())
        assert env.scan_calls == [] and "warm task not scheduled" in caplog.text


class TestWarmMomentumMoversCache:
    @pytest.fixture
    def env(self, monkeypatch, lenv):
        e = types.SimpleNamespace(calls=[], movers_raises=None, universe_raises=None)

        def movers():
            e.calls.append("movers")
            if e.movers_raises:
                raise e.movers_raises

        def universe():
            e.calls.append("universe")
            if e.universe_raises:
                raise e.universe_raises

        monkeypatch.setattr(gw, "_get_momentum_movers", movers)
        monkeypatch.setattr(gw, "_build_scan_universe", universe)
        return e

    def _go(self):
        async def go():
            await gw._warm_momentum_movers_cache()
            await _drain()()
        _run(go())

    def test_movers_then_the_scan_universe_are_warmed_in_the_background(self, env, caplog):
        async def go():
            await gw._warm_momentum_movers_cache()
            assert env.calls == []                       # scheduled, not awaited
            await _drain()()

        with caplog.at_level("INFO"):
            _run(go())
        assert env.calls == ["movers", "universe"]
        assert "momentum-movers cache pre-warmed" in caplog.text and "scan-universe cache pre-warmed" in caplog.text

    def test_a_movers_failure_skips_the_universe_warm(self, env, caplog):
        env.movers_raises = RuntimeError("nse down")
        with caplog.at_level("WARNING"):
            self._go()
        assert env.calls == ["movers"] and "momentum-movers warm, non-fatal): nse down" in caplog.text

    def test_a_universe_failure_is_a_non_fatal_warning(self, env, caplog):
        env.universe_raises = RuntimeError("universe down")
        with caplog.at_level("WARNING"):
            self._go()
        assert env.calls == ["movers", "universe"] and "scan-universe warm, non-fatal): universe down" in caplog.text

    def test_a_scheduling_failure_is_swallowed(self, env, lenv, caplog):
        lenv.create_task_raises = True
        with caplog.at_level("DEBUG"):
            _run(gw._warm_momentum_movers_cache())
        assert env.calls == [] and "warm task not scheduled" in caplog.text
