"""Round 640 (E666): screening.prev_day.get gives its in-flight slot back when the fetch thread cannot be started.

`get` adds the symbol to `_inflight` and then starts the fetch thread; only that thread's `finally` removes it. If
`Thread.start()` raised ("can't start new thread"), the outer `except` swallowed it and the symbol stayed in
`_inflight` for good: that symbol never got its previous-day facts again, and after PREVDAY_MAX_INFLIGHT such failures
no symbol could start a fetch at all (the opening gate then ran without previous-day data until a restart).
Now a failed start frees the slot, keeps the retry window, and logs one warning a minute at most.

Plain runner: `python3 tests/<this file>` (also collected by pytest). No network.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from screening import prev_day as PD  # noqa: E402


class _Boom:
    def __init__(self, *a, **k):
        pass

    def start(self):
        raise RuntimeError("can't start new thread")


class _Rig:
    def __enter__(self):
        PD.reset()
        self.saved = (threading.Thread, PD._INLINE, PD._fetch_candles)
        PD._INLINE = False
        self.log = []

        class _H(logging.Handler):
            def emit(h, record):
                self.log.append(record.getMessage())
        self.h = _H(level=logging.WARNING)
        PD.logger.addHandler(self.h)
        return self

    def __exit__(self, *a):
        threading.Thread, PD._INLINE, PD._fetch_candles = self.saved
        PD.logger.removeHandler(self.h)
        PD.reset()


def test_failed_start_frees_the_slot_and_returns_none():
    with _Rig():
        threading.Thread = _Boom
        assert PD.get("AAA") is None
        assert "AAA" not in PD._inflight


def test_repeated_failures_do_not_fill_every_slot():
    with _Rig():
        threading.Thread = _Boom
        for i in range(PD.PREVDAY_MAX_INFLIGHT * 3):
            assert PD.get(f"S{i}") is None
        assert len(PD._inflight) == 0


def test_the_retry_window_is_kept_then_the_next_call_starts_a_fetch():
    with _Rig():
        started = []

        class _Spy:
            def __init__(self, *a, **k):
                self.args = k.get("args")

            def start(self):
                started.append(self.args)

        threading.Thread = _Boom
        PD.get("AAA")
        threading.Thread = _Spy
        PD.get("AAA")                                   # inside PREVDAY_RETRY_S: nothing starts
        assert started == []
        PD._last_try["AAA"] = time.monotonic() - PD.PREVDAY_RETRY_S - 1
        PD.get("AAA")
        assert started == [("AAA",)] and "AAA" in PD._inflight   # the (spy) thread owns the slot again


def test_one_warning_per_minute_not_one_per_symbol():
    with _Rig() as r:
        threading.Thread = _Boom
        for i in range(5):
            PD.get(f"S{i}")
        assert len(r.log) == 1 and "could not start a fetch thread" in r.log[0]


def test_normal_start_still_fetches_and_releases_the_slot():
    with _Rig():
        PD._fetch_candles = lambda sym: []
        done = threading.Event()
        real_worker = PD._worker

        def wrapped(sym):
            real_worker(sym)
            done.set()
        PD._worker = wrapped
        try:
            assert PD.get("AAA") is None
            assert done.wait(2.0)
        finally:
            PD._worker = real_worker
        time.sleep(0.05)
        assert "AAA" not in PD._inflight


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(e))
    sys.exit(1 if fails else 0)
