"""
group204 (open item 5, duplicate Yahoo WebSocket): the boot log showed "yahoo_ws_feed: subscribed to 500
symbols" twice about a minute apart. Code paths that could open a second socket / re-subscribe the same
universe, all closed here:

  * start_feed_background() checked "is a thread alive" and started the thread as two separate steps, and
    it is called from a worker thread (boot fallback) AND from the event loop (universe refresh);
  * a listen() that RETURNED (instead of raising) fell straight back to the top of the loop: a new
    AsyncWebSocket, a full re-subscribe, no delay, no log line, and the old socket was never closed;
  * the connection loop subscribed only the list the thread was first started with, so a universe pushed
    in by the 20 s refresh while the feed was idle or reconnecting was dropped;
  * ensure_subscribed() could send the whole universe again onto a socket still doing its first subscribe.

Everything is faked: no sockets, no real yfinance, no real sleeping.
"""
import asyncio
import logging
import os
import sys
import threading
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import yahoo_ws_feed as yw

_REAL_SLEEP = asyncio.sleep


@pytest.fixture()
def fresh(monkeypatch):
    """Reset the module's process-wide state around each test."""
    monkeypatch.setattr(yw, "_LIVE", {})
    monkeypatch.setattr(yw, "_STATE", {
        "connected": False, "started": False, "subscribed": [], "last_message_at": 0.0,
        "error": None, "reconnects": 0, "connects": 0,
    })
    monkeypatch.setattr(yw, "_DESIRED", set())
    monkeypatch.setattr(yw, "_THREAD", None)
    monkeypatch.setattr(yw, "_LOOP", None)
    monkeypatch.setattr(yw, "_WS_CLIENT", None)
    monkeypatch.setattr(yw, "MIN_RECONNECT_GAP_S", 0.0)
    monkeypatch.setattr(yw, "IDLE_RECHECK_S", 0.0)
    return monkeypatch


class FakeWS:
    """Stands in for yfinance.AsyncWebSocket. `plan` is consumed one entry per instance's listen()."""
    instances = []
    plan = []
    subscribe_gate = None   # optional asyncio.Event the FIRST subscribe waits on
    subscribe_raises = []   # one entry consumed per instance: True = that instance's first subscribe raises
    release = None          # asyncio.Event used by the "wait_then_return" plan step

    def __init__(self, verbose=False):
        self.subscribed_calls = []
        self.closed = False
        self.idx = len(FakeWS.instances)
        FakeWS.instances.append(self)

    async def subscribe(self, symbols):
        self.subscribed_calls.append(list(symbols))
        if len(self.subscribed_calls) == 1:
            if FakeWS.subscribe_raises and FakeWS.subscribe_raises.pop(0):
                raise RuntimeError("connect failed")
            if FakeWS.subscribe_gate is not None:
                await FakeWS.subscribe_gate.wait()

    async def listen(self, handler):
        step = FakeWS.plan.pop(0) if FakeWS.plan else "block"
        if step == "return":
            return
        if step == "raise":
            raise RuntimeError("socket dropped")
        if step == "wait_then_return":
            await FakeWS.release.wait()
            return
        await asyncio.Event().wait()   # block until the task is cancelled

    async def close(self):
        self.closed = True


@pytest.fixture()
def fakeyf(fresh):
    FakeWS.instances = []
    FakeWS.plan = []
    FakeWS.subscribe_gate = None
    FakeWS.subscribe_raises = []
    FakeWS.release = None
    yf = types.ModuleType("yfinance")
    yf.AsyncWebSocket = FakeWS
    fresh.setitem(sys.modules, "yfinance", yf)
    fresh.setattr(yw, "is_feed_window_ist", lambda *a, **k: True)
    # the 5 s crash back-off must not really sleep
    fresh.setattr(yw.asyncio, "sleep", lambda s, *a, **k: _REAL_SLEEP(0))
    return fresh


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _capture():
    h = _Capture()
    yw.logger.addHandler(h)
    prev = yw.logger.level
    yw.logger.setLevel(logging.DEBUG)
    return h, lambda: (yw.logger.removeHandler(h), yw.logger.setLevel(prev))


async def _until(cond, tries=400):
    for _ in range(tries):
        if cond():
            return True
        await _REAL_SLEEP(0)
    return False


async def _run_loop(universe, body):
    task = asyncio.ensure_future(yw._async_feed_main(universe))
    try:
        await body()
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------- start_feed_background

def test_concurrent_start_opens_one_thread(fresh):
    started = []
    release = threading.Event()

    def fake_run(universe):
        started.append(threading.current_thread().name)
        release.wait(5)

    fresh.setattr(yw, "_run_feed_thread", fake_run)
    barrier = threading.Barrier(8)

    def caller():
        barrier.wait()
        yw.start_feed_background(["RELIANCE", "TCS"])

    threads = [threading.Thread(target=caller) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    try:
        # give the one real thread a moment to run its first line
        for _ in range(200):
            if started:
                break
            threading.Event().wait(0.005)
        assert len(started) == 1
    finally:
        release.set()
        if yw._THREAD:
            yw._THREAD.join(5)


def test_start_when_running_adds_symbols_without_a_second_thread(fresh):
    release = threading.Event()
    calls = []
    fresh.setattr(yw, "_run_feed_thread", lambda u: (calls.append(1), release.wait(5)))
    yw.start_feed_background(["AAA"])
    yw.start_feed_background(["BBB", "ccc.ns"])
    try:
        assert yw._DESIRED == {"AAA.NS", "BBB.NS", "CCC.NS"}
        for _ in range(200):
            if calls:
                break
            threading.Event().wait(0.005)
        assert len(calls) == 1
    finally:
        release.set()
        yw._THREAD.join(5)


def test_start_with_empty_universe_does_not_start(fresh):
    yw.start_feed_background([])
    assert yw._THREAD is None


# ---------------------------------------------------------------- ensure_subscribed

def test_ensure_subscribed_without_client_records_desired(fresh):
    yw.ensure_subscribed(["abc", "XYZ.NS", "", None])
    assert yw._DESIRED == {"ABC.NS", "XYZ.NS"}


# ---------------------------------------------------------------- the connection loop

def test_normal_run_opens_exactly_one_socket_and_subscribes_once(fakeyf):
    async def body():
        assert await _until(lambda: yw._STATE["connects"] == 1)
        await _REAL_SLEEP(0)
        await _REAL_SLEEP(0)
    asyncio.run(_run_loop(["AAA", "BBB"], body))
    assert len(FakeWS.instances) == 1
    assert FakeWS.instances[0].subscribed_calls == [["AAA.NS", "BBB.NS"]]


def test_listen_returning_closes_old_socket_logs_and_reconnects(fakeyf):
    FakeWS.plan = ["return", "block"]
    h, undo = _capture()
    try:
        async def body():
            assert await _until(lambda: yw._STATE["connects"] == 2)
        asyncio.run(_run_loop(["AAA"], body))
    finally:
        undo()
    assert len(FakeWS.instances) == 2
    assert FakeWS.instances[0].closed is True          # the first socket is not leaked
    text = "\n".join(h.lines)
    assert "listen() returned" in text
    assert "connection #1, initial connect" in text
    assert "connection #2, after listen() returned" in text


def test_crash_closes_old_socket_and_resubscribes_everything(fakeyf):
    FakeWS.plan = ["raise", "block"]
    h, undo = _capture()
    try:
        async def body():
            assert await _until(lambda: yw._STATE["connects"] == 2)
        asyncio.run(_run_loop(["AAA", "BBB"], body))
    finally:
        undo()
    assert FakeWS.instances[0].closed is True
    assert FakeWS.instances[1].subscribed_calls[0] == ["AAA.NS", "BBB.NS"]
    assert yw._STATE["reconnects"] == 1
    assert "connection #2, after a crash" in "\n".join(h.lines)


def test_failed_first_subscribe_closes_the_half_open_socket(fakeyf):
    FakeWS.subscribe_raises = [True]
    FakeWS.plan = ["block"]

    async def body():
        assert await _until(lambda: yw._STATE["connects"] == 1)
    asyncio.run(_run_loop(["AAA"], body))
    assert len(FakeWS.instances) == 2
    assert FakeWS.instances[0].closed is True


def test_reconnect_subscribes_symbols_added_by_the_refresh(fakeyf):
    """The refresh adds symbols while the feed is connected; after a drop the new connection must carry
    them (it used to fall back to the list the thread was first started with)."""
    FakeWS.plan = ["wait_then_return", "block"]

    async def body():
        FakeWS.release = asyncio.Event()
        assert await _until(lambda: yw._STATE["connects"] == 1)
        yw.ensure_subscribed(["NEW1", "NEW2"])           # records them (no loop handle in this fake)
        FakeWS.release.set()                              # connection #1 ends
        assert await _until(lambda: yw._STATE["connects"] == 2)
    asyncio.run(_run_loop(["AAA"], body))
    assert FakeWS.instances[1].subscribed_calls[0] == ["AAA.NS", "NEW1.NS", "NEW2.NS"]


def test_symbols_added_while_idle_are_subscribed_when_the_window_opens(fakeyf):
    window = {"open": False}
    fakeyf.setattr(yw, "is_feed_window_ist", lambda *a, **k: window["open"])

    async def body():
        await _until(lambda: False, tries=20)           # let it idle a few turns
        assert FakeWS.instances == []
        yw.ensure_subscribed(["LATE"])                   # no client: only recorded
        window["open"] = True
        assert await _until(lambda: yw._STATE["connects"] == 1)
    asyncio.run(_run_loop(["AAA"], body))
    assert FakeWS.instances[0].subscribed_calls[0] == ["AAA.NS", "LATE.NS"]


def test_market_close_closes_the_socket_and_next_open_logs_the_reason(fakeyf):
    window = {"open": True}
    fakeyf.setattr(yw, "is_feed_window_ist", lambda *a, **k: window["open"])
    FakeWS.plan = ["wait_then_return", "block"]
    h, undo = _capture()
    try:
        async def body():
            FakeWS.release = asyncio.Event()
            assert await _until(lambda: yw._STATE["connects"] == 1)
            window["open"] = False
            FakeWS.release.set()                          # connection #1 ends right as the window closes
            assert await _until(lambda: FakeWS.instances[0].closed)
            assert yw._STATE["subscribed"] == [] and yw._STATE["connected"] is False
            assert len(FakeWS.instances) == 1             # idle: no second socket while closed
            window["open"] = True
            assert await _until(lambda: yw._STATE["connects"] == 2)
        asyncio.run(_run_loop(["AAA"], body))
    finally:
        undo()
    assert "connection #2, market window opened" in "\n".join(h.lines)


def test_symbol_added_during_first_subscribe_is_sent_once_not_the_whole_universe_again(fakeyf):
    FakeWS.plan = ["block"]

    async def body():
        FakeWS.subscribe_gate = asyncio.Event()
        task_ready = await _until(lambda: len(FakeWS.instances) == 1)
        assert task_ready
        # the first subscribe is still in flight: _WS_CLIENT is not published yet
        assert yw._WS_CLIENT is None
        yw.ensure_subscribed(["MID"])
        FakeWS.subscribe_gate.set()
        assert await _until(lambda: len(FakeWS.instances[0].subscribed_calls) == 2)
    asyncio.run(_run_loop(["AAA", "BBB"], body))
    calls = FakeWS.instances[0].subscribed_calls
    assert calls[0] == ["AAA.NS", "BBB.NS"]
    assert calls[1] == ["MID.NS"]          # only the newcomer, never the full list a second time
    assert yw._STATE["subscribed"] == ["AAA.NS", "BBB.NS", "MID.NS"]


# ---------------------------------------------------------------- status / thread death

def test_feed_status_reports_connects_and_desired(fakeyf):
    async def body():
        assert await _until(lambda: yw._STATE["connects"] == 1)
        st = yw.feed_status()
        assert st["connects"] == 1
        assert st["desired_count"] == 2
        assert st["subscribed_count"] == 2
    asyncio.run(_run_loop(["AAA", "BBB"], body))


def test_thread_death_clears_the_client(fresh):
    fresh.setattr(yw, "_WS_CLIENT", object())

    async def boom(universe):
        raise RuntimeError("x")

    fresh.setattr(yw, "_async_feed_main", boom)
    yw._run_feed_thread(["AAA"])
    assert yw._WS_CLIENT is None
    assert yw._STATE["connected"] is False
