"""try_acquire must shed a concurrent burst instead of letting everyone through at max_wait."""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import rate_limiter  # noqa: E402


def test_burst_is_shed_not_released_all_at_once(monkeypatch):
    monkeypatch.setenv("RL_TESTPROV_RPS", "5")
    monkeypatch.setenv("RL_TESTPROV_BURST", "3")
    rate_limiter._buckets.pop("testprov", None)
    passed, shed = [], []

    def worker():
        (passed if rate_limiter.try_acquire("testprov", 1, max_wait=1.0) else shed).append(1)

    ts = [threading.Thread(target=worker) for _ in range(120)]
    t0 = time.time()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    took = time.time() - t0
    # burst(3) + 5/s * ~1s ≈ 8 tokens; allow scheduling slack, but nowhere near 120
    assert 3 <= len(passed) <= 14, len(passed)
    assert len(shed) >= 100
    assert took < 3.0


def test_legacy_acquire_still_fails_open(monkeypatch):
    monkeypatch.setenv("RL_TESTPROV2_RPS", "0.2")
    monkeypatch.setenv("RL_TESTPROV2_BURST", "1")
    rate_limiter._buckets.pop("testprov2", None)
    rate_limiter.acquire("testprov2", 1)              # uses the single burst token
    t0 = time.time()
    waited = rate_limiter.acquire("testprov2", 1, max_wait=0.3)   # needs 5s of refill → let through anyway
    assert waited >= 0.3 and time.time() - t0 < 3.5


def test_get_candles_sheds_instead_of_calling_angelone_when_no_token(monkeypatch):
    import asyncio
    import angelone_client as ac

    session = ac.AngelOneSession()

    async def _no_login():
        return None

    monkeypatch.setattr(session, "ensure_session", _no_login)
    monkeypatch.setattr(ac, "_rl_in_cooldown", lambda p: False)
    monkeypatch.setattr(ac, "_rl_try_acquire", lambda *a, **k: False)   # limiter says: shed

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("must not open an HTTP connection when the limiter sheds the call")

    monkeypatch.setattr(ac.httpx, "AsyncClient", _Boom)
    assert asyncio.run(session.get_candles("NSE", "123", "ONE_DAY", "2026-08-01 09:15", "2026-09-01 15:30")) == []
