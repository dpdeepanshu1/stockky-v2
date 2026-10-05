"""
group173 (item 7 of the open list, boot burst): the live feeds used to start at boot on the default universe
(~250 symbols) and ~20 s later restart on the first /scan/universe answer (~491 symbols): two AngelOne logins
and two subscribe waves in the first minute. With API_GATEWAY_URL set the boot hooks now wait for the first
universe and start each feed once with it; if none arrives within FEED_BOOT_UNIVERSE_WAIT_S the feeds start on
the default universe as before. FEED_BOOT_WAIT_FOR_UNIVERSE=0 restores the old behaviour.
Everything is faked: no sockets, no threads, no real sleeping.
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main as m


def run(c):
    return asyncio.run(c)


@pytest.fixture()
def gw(monkeypatch):
    monkeypatch.setenv("API_GATEWAY_URL", "http://gw:1")
    monkeypatch.delenv("FEED_BOOT_UNIVERSE_WAIT_S", raising=False)
    monkeypatch.setattr(m, "FEED_BOOT_WAIT_FOR_UNIVERSE", True)


# ── when to defer ────────────────────────────────────────────────────────────

def test_defers_when_gateway_set(gw):
    assert m._boot_defers_feeds() is True


@pytest.mark.parametrize("raw", ["", "   ", "\n"])
def test_does_not_defer_without_a_gateway_url(monkeypatch, raw):
    monkeypatch.setenv("API_GATEWAY_URL", raw)
    monkeypatch.setattr(m, "FEED_BOOT_WAIT_FOR_UNIVERSE", True)
    assert m._boot_defers_feeds() is False


def test_switch_off_restores_the_old_immediate_start(gw, monkeypatch):
    monkeypatch.setattr(m, "FEED_BOOT_WAIT_FOR_UNIVERSE", False)
    assert m._boot_defers_feeds() is False


def test_zero_wait_means_no_deferral(gw, monkeypatch):
    monkeypatch.setenv("FEED_BOOT_UNIVERSE_WAIT_S", "0")
    assert m._boot_defers_feeds() is False


@pytest.mark.parametrize("raw,expect", [("", 60.0), ("45", 45.0), (" 30 ", 30.0), ("abc", 60.0), ("-5", 60.0), ("nan", 60.0)])
def test_wait_seconds_parsing(monkeypatch, raw, expect):
    monkeypatch.setenv("FEED_BOOT_UNIVERSE_WAIT_S", raw)
    assert m._feed_boot_wait_s() == expect


# ── startup hooks ────────────────────────────────────────────────────────────

@pytest.fixture()
def starts(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_boot_start_angelone_feed", lambda: calls.append("angelone"))
    monkeypatch.setattr(m, "_boot_start_yahoo_feed", lambda: calls.append("yahoo"))
    return calls


def test_hooks_do_not_start_the_feeds_while_deferring(gw, starts):
    run(m._start_angelone_ws_feed())
    run(m._start_yahoo_ws_feed())
    assert starts == []


def test_hooks_start_immediately_when_not_deferring(monkeypatch, starts):
    monkeypatch.setenv("API_GATEWAY_URL", "")
    run(m._start_angelone_ws_feed())
    run(m._start_yahoo_ws_feed())
    assert starts == ["angelone", "yahoo"]


def test_fallback_task_is_only_created_when_deferring(gw, monkeypatch):
    made = []

    def fake_create_task(coro):
        made.append(coro)
        coro.close()
    monkeypatch.setattr(m.asyncio, "create_task", fake_create_task)
    run(m._start_boot_feed_fallback())
    assert len(made) == 1
    monkeypatch.setenv("API_GATEWAY_URL", "")
    run(m._start_boot_feed_fallback())
    assert len(made) == 1


# ── fallback after the wait ──────────────────────────────────────────────────

def _no_sleep(monkeypatch):
    waited = []

    async def _sleep(d):
        waited.append(d)
    monkeypatch.setattr(m.asyncio, "sleep", _sleep)
    return waited


def test_fallback_starts_both_feeds_when_no_universe_arrived(gw, starts, monkeypatch, caplog):
    waited = _no_sleep(monkeypatch)
    monkeypatch.setattr(m, "_current_feed_universe", [])
    with caplog.at_level("INFO"):
        run(m._boot_feed_fallback())
    assert waited == [60.0] and starts == ["angelone", "yahoo"]
    assert any("no scan universe after 60s" in r.getMessage() for r in caplog.records)


def test_fallback_does_nothing_when_the_universe_already_arrived(gw, starts, monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setattr(m, "_current_feed_universe", ["TCS"])
    run(m._boot_feed_fallback())
    assert starts == []


def test_fallback_uses_the_configured_wait(gw, starts, monkeypatch):
    waited = _no_sleep(monkeypatch)
    monkeypatch.setenv("FEED_BOOT_UNIVERSE_WAIT_S", "12")
    monkeypatch.setattr(m, "_current_feed_universe", ["TCS"])
    run(m._boot_feed_fallback())
    assert waited == [12.0]


# ── the default-universe starters ────────────────────────────────────────────

def _fake_modules(monkeypatch, configured=True, universe=("TCS", "INFY")):
    started = {"angel": [], "yahoo": []}
    ac = types.ModuleType("angelone_client")
    ac.get_session = lambda: types.SimpleNamespace(is_configured=lambda: configured)
    aw = types.ModuleType("angelone_ws_feed")
    aw.start_feed_background = lambda u: started["angel"].append(list(u))
    yw = types.ModuleType("yahoo_ws_feed")
    yw.start_feed_background = lambda u: started["yahoo"].append(list(u))
    sp = types.ModuleType("surprise_premarket")
    sp.default_universe_from_env = lambda: list(universe)
    for name, mod in (("angelone_client", ac), ("angelone_ws_feed", aw), ("yahoo_ws_feed", yw), ("surprise_premarket", sp)):
        monkeypatch.setitem(sys.modules, name, mod)
    return started


def test_angelone_starter_starts_on_the_default_universe(monkeypatch):
    started = _fake_modules(monkeypatch)
    m._boot_start_angelone_feed()
    assert started["angel"] == [["TCS", "INFY"]]


def test_angelone_starter_skips_when_unconfigured(monkeypatch, caplog):
    started = _fake_modules(monkeypatch, configured=False)
    with caplog.at_level("WARNING"):
        m._boot_start_angelone_feed()
    assert started["angel"] == [] and "ANGELONE_* env vars not set" in caplog.text


def test_starters_warn_on_an_empty_universe(monkeypatch, caplog):
    started = _fake_modules(monkeypatch, universe=())
    with caplog.at_level("WARNING"):
        m._boot_start_angelone_feed()
        m._boot_start_yahoo_feed()
    assert started == {"angel": [], "yahoo": []}
    assert "no universe configured" in caplog.text


def test_yahoo_starter_starts_on_the_default_universe(monkeypatch):
    started = _fake_modules(monkeypatch)
    m._boot_start_yahoo_feed()
    assert started["yahoo"] == [["TCS", "INFY"]]


def test_starters_never_raise(monkeypatch, caplog):
    sp = types.ModuleType("surprise_premarket")

    def boom():
        raise RuntimeError("boom")
    sp.default_universe_from_env = boom
    monkeypatch.setitem(sys.modules, "surprise_premarket", sp)
    ac = types.ModuleType("angelone_client")
    ac.get_session = lambda: types.SimpleNamespace(is_configured=lambda: True)
    monkeypatch.setitem(sys.modules, "angelone_client", ac)
    with caplog.at_level("WARNING"):
        m._boot_start_angelone_feed()
        m._boot_start_yahoo_feed()
    assert "startup skipped" in caplog.text


# ── refresh loop starts the feeds that boot left for it ──────────────────────

def test_first_universe_starts_yahoo_then_subscribes_and_restarts_angelone_once(monkeypatch):
    monkeypatch.setenv("API_GATEWAY_URL", "http://gw:1")
    monkeypatch.setenv("FEED_UNIVERSE_INITIAL_DELAY_S", "0")
    monkeypatch.setattr(m, "_current_feed_universe", [])
    order = []
    yw = types.ModuleType("yahoo_ws_feed")
    yw.start_feed_background = lambda u: order.append(("yahoo_start", len(u)))
    yw.ensure_subscribed = lambda u: order.append(("yahoo_sub", len(u)))
    ac = types.ModuleType("angelone_client")
    ac.get_session = lambda: types.SimpleNamespace(is_configured=lambda: True)
    aw = types.ModuleType("angelone_ws_feed")
    aw.stop_feed_background = lambda *a, **k: order.append(("angel_stop",))
    aw.start_feed_background = lambda u: order.append(("angel_start", len(u)))
    for name, mod in (("yahoo_ws_feed", yw), ("angelone_client", ac), ("angelone_ws_feed", aw)):
        monkeypatch.setitem(sys.modules, name, mod)

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"symbols": ["TCS.NS", "INFY", "WIPRO"]}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            return _Resp()

    class _Stop(Exception):
        pass
    n = {"c": 0}

    async def _sleep(d):
        n["c"] += 1
        if n["c"] > 1:
            raise _Stop()
    monkeypatch.setattr(m.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(m.asyncio, "sleep", _sleep)
    with pytest.raises(_Stop):
        run(m._refresh_feed_universe_loop())
    assert order == [("yahoo_start", 3), ("yahoo_sub", 3), ("angel_stop",), ("angel_start", 3)]


def test_refresh_loop_tolerates_a_yahoo_module_without_start(monkeypatch):
    monkeypatch.setenv("API_GATEWAY_URL", "http://gw:1")
    monkeypatch.setenv("FEED_UNIVERSE_INITIAL_DELAY_S", "0")
    monkeypatch.setattr(m, "_current_feed_universe", [])
    seen = []
    yw = types.ModuleType("yahoo_ws_feed")
    yw.ensure_subscribed = lambda u: seen.append(len(u))
    ac = types.ModuleType("angelone_client")
    ac.get_session = lambda: types.SimpleNamespace(is_configured=lambda: False)
    monkeypatch.setitem(sys.modules, "yahoo_ws_feed", yw)
    monkeypatch.setitem(sys.modules, "angelone_client", ac)

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"symbols": ["TCS"]}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            return _Resp()

    class _Stop(Exception):
        pass
    n = {"c": 0}

    async def _sleep(d):
        n["c"] += 1
        if n["c"] > 1:
            raise _Stop()
    monkeypatch.setattr(m.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(m.asyncio, "sleep", _sleep)
    with pytest.raises(_Stop):
        run(m._refresh_feed_universe_loop())
    assert seen == [1]
