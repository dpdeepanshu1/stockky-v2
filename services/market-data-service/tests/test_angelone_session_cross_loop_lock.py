"""
tests/test_angelone_session_cross_loop_lock.py

2026-09-21 regression test for the "is bound to a different event loop"
crash seen in market-data-service logs:

    ERROR:angelone-ws-feed:AngelOne feed error: <asyncio.locks.Lock
    object at 0x... [unlocked, waiters:1]> is bound to a different
    event loop

Root cause: AngelOneSession (angelone_client.py) is a module-level
singleton used from two different event loops in the same process —
the main uvicorn loop (FastAPI request handlers) and
angelone_ws_feed.py's dedicated background-thread loop. A single shared
asyncio.Lock() lazily binds to whichever loop first acquires it; any
await from the other loop then raises RuntimeError.

This test reproduces the exact shape of that bug at the lock level
(two real, separate event loops, each acquiring the session's lock)
without needing real AngelOne credentials or network access — it only
exercises AngelOneSession._get_lock()/ensure_session()'s locking, with
_login() mocked out so no HTTP call happens.

Run from services/market-data-service:
    python -m pytest tests/test_angelone_session_cross_loop_lock.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import angelone_client


def test_ensure_session_lock_survives_two_separate_event_loops():
    """Before the fix, acquiring the session's lock from a second,
    independent event loop (exactly what angelone_ws_feed.py's
    dedicated thread does) raised:
        RuntimeError: <asyncio.locks.Lock ...> is bound to a different
        event loop
    After the fix (one lock per event loop, keyed by the loop object),
    both loops can acquire it without error."""
    session = angelone_client.AngelOneSession()

    # Make ensure_session() a no-op HTTP-wise: pretend we're always
    # "already logged in" so the lock's critical section is exercised
    # without needing real credentials or a network call.
    from datetime import datetime, timedelta
    session.token = "fake-token"
    session.token_expiry = datetime.utcnow() + timedelta(hours=1)

    errors: list[BaseException] = []

    def _run_ensure_session_in_new_loop():
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(session.ensure_session())
        except BaseException as e:  # capture for the main thread to assert on
            errors.append(e)
        finally:
            loop.close()

    # First loop: the "main uvicorn loop" stand-in, right here.
    asyncio.run(session.ensure_session())

    # Second loop: the "angelone_ws_feed.py background thread" stand-in —
    # a genuinely different event loop, on a different thread, exactly
    # like the real bug.
    t = threading.Thread(target=_run_ensure_session_in_new_loop)
    t.start()
    t.join(timeout=10)

    assert not errors, f"ensure_session() raised on the second event loop: {errors!r}"
    # Confirm we actually created two distinct per-loop locks, not one
    # shared lock silently working by luck.
    assert len(session._locks) == 2


if __name__ == "__main__":
    test_ensure_session_lock_survives_two_separate_event_loops()
    print("OK")
