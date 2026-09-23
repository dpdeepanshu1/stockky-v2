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

    # Keep both loops alive (referenced) for the duration of the test so
    # the WeakKeyDictionary doesn't drop their entries before we can
    # assert on them — in real usage that dropping is exactly the point
    # (see test_locks_dict_does_not_leak_one_shot_asyncio_run_loops
    # below), but here we want to see both entries present at once to
    # prove two distinct per-loop locks were actually created.
    loop1 = asyncio.new_event_loop()
    loop2 = asyncio.new_event_loop()

    def _run_ensure_session_in_new_loop(loop):
        try:
            loop.run_until_complete(session.ensure_session())
        except BaseException as e:  # capture for the main thread to assert on
            errors.append(e)

    # "Main uvicorn loop" stand-in.
    _run_ensure_session_in_new_loop(loop1)

    # "angelone_ws_feed.py background thread" stand-in — a genuinely
    # different event loop, on a different thread, exactly like the
    # real bug.
    t = threading.Thread(target=_run_ensure_session_in_new_loop, args=(loop2,))
    t.start()
    t.join(timeout=10)

    assert not errors, f"ensure_session() raised on the second event loop: {errors!r}"
    # Confirm we actually created two distinct per-loop locks, not one
    # shared lock silently working by luck — checked before either loop
    # is closed/dereferenced, since the WeakKeyDictionary is entitled to
    # drop an entry the moment nothing references its loop anymore.
    assert len(session._locks) == 2

    loop1.close()
    loop2.close()


def test_locks_dict_does_not_leak_one_shot_asyncio_run_loops():
    """Regression for the memory leak the first fix introduced: main.py
    has several sync route handlers (/angelone/movers, per-request
    quote/candle lookups) that call asyncio.run(...) — each call spins
    up a brand-new, one-shot event loop that's discarded the instant the
    call returns. A plain dict for self._locks would pin every one of
    those throwaway loops in memory forever, one entry per request.
    self._locks must be a WeakKeyDictionary so an entry disappears once
    its loop is no longer referenced anywhere else."""
    import gc

    session = angelone_client.AngelOneSession()
    from datetime import datetime, timedelta
    session.token = "fake-token"
    session.token_expiry = datetime.utcnow() + timedelta(hours=1)

    # Simulate 10 separate one-shot asyncio.run() calls, exactly like
    # /angelone/movers or the per-request quote/candle routes in main.py.
    for _ in range(10):
        asyncio.run(session.ensure_session())

    gc.collect()
    assert len(session._locks) == 0, (
        f"expected all one-shot event loops to be garbage-collected and "
        f"their lock entries dropped, but {len(session._locks)} remain — "
        f"self._locks is leaking memory again"
    )


if __name__ == "__main__":
    test_ensure_session_lock_survives_two_separate_event_loops()
    print("OK")
