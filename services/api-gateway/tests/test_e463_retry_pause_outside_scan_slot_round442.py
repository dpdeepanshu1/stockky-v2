"""E463: `_analyze_one_symbol_ultra` takes its retry pause with the scan slot released, and does not retry a 4xx the decision service answered."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from test_main_analyze_symbol import env, log, gw, DResp  # noqa: F401  (fixtures + the module under test)


def _status_error(code):
    req = httpx.Request("GET", "http://d")
    return httpx.HTTPStatusError(str(code), request=req, response=httpx.Response(code, request=req))


def _run(env, monkeypatch, symbol="TCS"):
    sem = asyncio.Semaphore(1)
    locked_at_sleep = []

    async def fake_sleep(delay, *a, **k):
        env.sleeps.append(delay)
        locked_at_sleep.append(sem.locked())

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def go():
        out = await gw._analyze_one_symbol_ultra(symbol, object(), sem)
        return out, sem.locked()

    out, after = asyncio.run(go())
    return out, locked_at_sleep, after


def test_the_retry_pause_runs_with_the_slot_released(env, monkeypatch):
    env.get_result = DResp(status_error=_status_error(503))
    out, at_sleep, after = _run(env, monkeypatch)
    assert out["decision"] == "ERROR" and len(env.get_calls) == 2
    assert env.sleeps == [1.0] and at_sleep == [False]      # was [True]: the pause ran inside `async with sem`
    assert after is False


def test_a_second_symbol_can_use_the_slot_while_the_first_waits_to_retry(env, monkeypatch):
    # One slot. TCS fails once and pauses; INFY must get the slot during that pause, not after TCS's retry.
    order = []
    good = env.get_result
    failed = {"done": False}

    async def get(client, name, url, timeout=5.0, **k):
        sym = url.rsplit("/", 1)[-1]
        order.append(("call", sym))
        if sym == "TCS" and not failed["done"]:
            failed["done"] = True
            raise httpx.ReadTimeout("slow")
        return good

    monkeypatch.setattr(gw, "_cb_get", get)

    async def real_pause(seconds):
        # a real suspension that does not depend on asyncio.sleep (the env fixture replaces that with a no-yield fake)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        loop.call_later(seconds, fut.set_result, None)
        await fut

    async def pause(delay, *a, **k):
        order.append(("sleep", delay))
        await real_pause(0.05)

    monkeypatch.setattr(asyncio, "sleep", pause)

    async def go():
        sem = asyncio.Semaphore(1)
        t1 = asyncio.ensure_future(gw._analyze_one_symbol_ultra("TCS", object(), sem))
        await real_pause(0.01)                                   # TCS has failed and is now in its pause
        t2 = asyncio.ensure_future(gw._analyze_one_symbol_ultra("INFY", object(), sem))
        return await asyncio.gather(t1, t2)

    a, b = asyncio.run(go())
    assert a["decision"] == "HOLD" and b["decision"] == "HOLD"
    tcs = [i for i, o in enumerate(order) if o == ("call", "TCS")]
    assert len(tcs) == 2
    assert tcs[0] < order.index(("call", "INFY")) < tcs[1]


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_a_4xx_from_the_engine_is_not_retried(env, monkeypatch, code):
    env.get_result = DResp(status_error=_status_error(code))
    out, at_sleep, _ = _run(env, monkeypatch)
    assert out == {"symbol": "TCS", "decision": "ERROR", "error": f"HTTPStatusError: {code}"}
    assert len(env.get_calls) == 1 and env.sleeps == []


@pytest.mark.parametrize("code", [408, 425, 429, 500, 502, 503, 504])
def test_transient_statuses_keep_their_one_retry(env, monkeypatch, code):
    env.get_result = DResp(status_error=_status_error(code))
    out, at_sleep, _ = _run(env, monkeypatch)
    assert out["decision"] == "ERROR" and len(env.get_calls) == 2 and env.sleeps == [1.0]


def test_network_errors_keep_their_retry_and_can_recover(env, monkeypatch):
    calls = {"n": 0}
    good = env.get_result

    async def flaky(client, name, url, timeout=5.0, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("down")
        return good

    monkeypatch.setattr(gw, "_cb_get", flaky)
    out, at_sleep, _ = _run(env, monkeypatch)
    assert out["decision"] == "HOLD" and calls["n"] == 2 and at_sleep == [False]


@pytest.mark.parametrize("exc,expected", [
    (_status_error(404), True), (_status_error(400), True), (_status_error(408), False), (_status_error(429), False),
    (_status_error(503), False), (httpx.ReadTimeout("x"), False), (RuntimeError("x"), False), (None, False),
])
def test_is_permanent_client_error(exc, expected):
    assert gw._is_permanent_client_error(exc) is expected
