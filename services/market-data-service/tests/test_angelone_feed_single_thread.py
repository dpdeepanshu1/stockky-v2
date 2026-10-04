"""
tests/test_angelone_feed_single_thread.py

2026-10-04 (log-audit item 26): "AngelOne feed: thread did not stop within 10.0s",
followed by a new thread starting -> two feed threads could poll at once.

Causes fixed in angelone_ws_feed.py:
  1. The off-hours idle wait was ONE 60s asyncio.sleep, so stop_feed_background()
     (10s join) always timed out off-hours. It now sleeps in 1s slices.
  2. A thread that outlived the join was re-enabled by the next start (shared
     _running flag set back to True) and its `finally` later cleared the flag
     under the new thread. A per-start generation number now supersedes it.

No network / no AngelOne credentials: angelone_client, angelone_scrip_master and
market_hours are replaced with stubs.

Run from services/market-data-service:
    python -m pytest tests/test_angelone_feed_single_thread.py -v
"""
from __future__ import annotations

import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


@pytest.fixture()
def feed(monkeypatch):
    mh = types.ModuleType("market_hours")
    mh.is_feed_window_ist = lambda: False          # off hours -> idle branch
    ac = types.ModuleType("angelone_client")

    class _Session:
        async def ensure_session(self):
            return None

        async def get_quotes_batch(self, *a, **k):
            return []

    ac.get_session = lambda: _Session()
    sm = types.ModuleType("angelone_scrip_master")
    sm.get_tokens_bulk = lambda syms, wait_s=0: {x: str(i) for i, x in enumerate(syms)}
    sm.status = lambda: {"loaded_symbols": 10}
    monkeypatch.setitem(sys.modules, "market_hours", mh)
    monkeypatch.setitem(sys.modules, "angelone_client", ac)
    monkeypatch.setitem(sys.modules, "angelone_scrip_master", sm)
    sys.modules.pop("angelone_ws_feed", None)
    import angelone_ws_feed as f
    f.IDLE_RECHECK_S = 60.0                         # the production value
    yield f
    f._running = False
    if f._thread is not None:
        f._thread.join(timeout=5)
    sys.modules.pop("angelone_ws_feed", None)


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def test_stop_during_off_hours_idle_returns_quickly(feed):
    feed.start_feed_background(["A", "B"])
    assert _wait(lambda: feed._thread is not None and feed._thread.is_alive())
    time.sleep(1.2)                                 # let it reach the 60s idle wait
    t0 = time.time()
    feed.stop_feed_background(timeout=10.0)
    assert time.time() - t0 < 5.0                   # was always the full 10s timeout
    assert not feed._thread.is_alive()
    assert feed._running is False


def test_start_after_timed_out_stop_leaves_one_feed_thread(feed):
    feed.start_feed_background(["A"])
    old = feed._thread
    time.sleep(1.0)
    feed._running = False                           # a stop whose join timed out
    feed.start_feed_background(["B"])               # old thread is still alive here
    new = feed._thread
    assert new is not old and feed._generation == 2
    assert _wait(lambda: not old.is_alive(), 5.0)   # superseded thread exits
    assert new.is_alive()                           # ...and the new one keeps running
    assert feed._running is True                    # old thread's finally did not clear it


def test_superseded_thread_does_not_clear_running_flag(feed):
    feed.start_feed_background(["A"])
    old = feed._thread
    time.sleep(0.5)
    feed._running = False
    feed.start_feed_background(["B"])
    old.join(timeout=5.0)
    assert feed._running is True


def test_start_is_still_idempotent_while_running(feed):
    feed.start_feed_background(["A"])
    first = feed._thread
    gen = feed._generation
    feed.start_feed_background(["A", "B"])
    assert feed._thread is first and feed._generation == gen
