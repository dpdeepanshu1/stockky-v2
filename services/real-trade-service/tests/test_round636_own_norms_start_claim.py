"""Round 636 (E662): the own-norms / own-spread loader claim is released when its thread cannot start.

`_norms` sets `_running = True` under the lock and only the loader thread sets it back to False. If `Thread.start()`
raised (for example "can't start new thread"), the exception was swallowed by the outer `except` and `_running` stayed
True for good: no later lookup started a loader, so the stock's own range / volume / spread norms never loaded again
and every gate silently used its constant until the service restarted.

Plain functions (pytest runs them unchanged; `python3 tests/test_round636_own_norms_start_claim.py` runs them without
pytest). No database, no network, no files.
"""
from __future__ import annotations

import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SVC = os.path.dirname(HERE)
if SVC not in sys.path:
    sys.path.insert(0, SVC)

import config  # noqa: E402,F401
from entry_engine import own_norms, own_spread  # noqa: E402


class _Boom:
    def __init__(self, *a, **k):
        pass

    def start(self):
        raise RuntimeError("can't start new thread")


def _arm(mod):
    """Clean state, feature on, build() replaced by a stub that needs no files."""
    mod.reset()
    saved = (mod.enabled, mod.build)
    mod.enabled = lambda: True
    mod.build = lambda sym, *a, **k: {"stub": sym}
    return saved


def _disarm(mod, saved):
    mod.enabled, mod.build = saved
    mod.reset()


def _wait(pred, secs=3.0):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _check_failed_start_frees_claim(mod):
    saved = _arm(mod)
    real = mod.threading.Thread
    mod.threading.Thread = _Boom
    try:
        assert mod._norms("ABC") is None                    # must not raise
    finally:
        mod.threading.Thread = real
    try:
        assert mod._running is False, "claim still held after a failed thread start"
        assert "ABC" in mod._pending, "the queued symbol must stay queued"
    finally:
        _disarm(mod, saved)


def _check_next_lookup_starts_a_loader(mod):
    saved = _arm(mod)
    real = mod.threading.Thread
    mod.threading.Thread = _Boom
    try:
        mod._norms("ABC")
    finally:
        mod.threading.Thread = real
    try:
        mod._norms("XYZ")                                   # real thread this time
        assert _wait(lambda: not mod._running and not mod._pending), "loader did not drain the queue"
        with mod._lock:
            assert mod._cache["ABC"][0] == {"stub": "ABC"}
            assert mod._cache["XYZ"][0] == {"stub": "XYZ"}
    finally:
        _disarm(mod, saved)


def _check_normal_start_still_claims_and_releases(mod):
    saved = _arm(mod)
    try:
        assert mod._norms("ABC") is None                    # first lookup: nothing cached yet
        assert _wait(lambda: not mod._running and not mod._pending)
        with mod._lock:
            assert mod._cache["ABC"][0] == {"stub": "ABC"}
        assert mod._norms("ABC") == {"stub": "ABC"}         # second lookup: served from cache
    finally:
        _disarm(mod, saved)


def test_norms_failed_start_frees_claim():
    _check_failed_start_frees_claim(own_norms)


def test_spread_failed_start_frees_claim():
    _check_failed_start_frees_claim(own_spread)


def test_norms_next_lookup_starts_a_loader():
    _check_next_lookup_starts_a_loader(own_norms)


def test_spread_next_lookup_starts_a_loader():
    _check_next_lookup_starts_a_loader(own_spread)


def test_norms_normal_start_still_works():
    _check_normal_start_still_claims_and_releases(own_norms)


def test_spread_normal_start_still_works():
    _check_normal_start_still_claims_and_releases(own_spread)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(n, "passed")
