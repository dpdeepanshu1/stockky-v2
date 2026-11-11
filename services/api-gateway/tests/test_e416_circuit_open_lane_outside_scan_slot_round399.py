"""E416: the circuit-open lane of `_analyze_one_symbol_ultra` (two pacing sleeps and a quote fetch) runs after the scan semaphore is released,
so symbols that hit an open circuit no longer hold one of the MAX_PARALLEL_WORKERS slots for 1.5-3.5 s plus the quote call. The row they return
is unchanged (pinned by test_main_analyze_symbol.py)."""
from __future__ import annotations

import asyncio

from test_main_analyze_symbol import env, log, gw  # noqa: F401  (fixtures + the module under test)


def _run_circuit(env, monkeypatch, retry_after=45.0):
    env.get_exc = gw.CircuitOpenError("decision", retry_after)
    env.price = 12.5
    sem = asyncio.Semaphore(1)
    locked_at_sleep, locked_at_quote = [], []

    async def fake_sleep(delay, *a, **k):
        env.sleeps.append(delay)
        locked_at_sleep.append(sem.locked())

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    real_quote = gw._fetch_price_from_quote

    def quote(sym):
        locked_at_quote.append(sem.locked())
        return real_quote(sym)

    monkeypatch.setattr(gw, "_fetch_price_from_quote", quote)

    async def go():
        out = await gw._analyze_one_symbol_ultra("TCS", object(), sem)
        return out, sem.locked()

    out, locked_after = asyncio.run(go())
    return out, locked_at_sleep, locked_at_quote, locked_after


def test_the_pacing_sleeps_and_the_quote_fetch_run_with_the_slot_released(env, monkeypatch):
    out, at_sleep, at_quote, after = _run_circuit(env, monkeypatch)
    assert out["circuit_open"] == "decision" and out["close"] == 12.5
    assert env.sleeps == [1.5, 2.0]
    assert at_sleep == [False, False]          # was [True, True]: both sleeps ran inside `async with sem`
    assert at_quote == [False]                 # was [True]: the quote call also held the slot
    assert after is False


def test_the_slot_is_free_again_after_a_circuit_open_symbol(env, monkeypatch):
    # With one slot, a second symbol must be able to take it straight after the circuit-open one returns.
    env.get_exc = gw.CircuitOpenError("decision", 5.0)
    sem = asyncio.Semaphore(1)

    async def go():
        a = await gw._analyze_one_symbol_ultra("TCS", object(), sem)
        b = await asyncio.wait_for(gw._analyze_one_symbol_ultra("INFY", object(), sem), timeout=5)
        return a, b

    a, b = asyncio.run(go())
    assert a["circuit_open"] == b["circuit_open"] == "decision"
