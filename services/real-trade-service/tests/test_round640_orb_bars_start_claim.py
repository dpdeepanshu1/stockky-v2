"""Round 640 (E666): entry_engine/orb_bars.get_bars gives its in-flight slot back when the fetch thread cannot be started.

`get_bars` adds the key to `_inflight` and then starts the `orb-bars` thread; only the worker's `_fetch_one` `finally`
removes it. If `Thread.start()` raised ("can't start new thread"), the outer `except` swallowed it and the key stayed in
`_inflight` for good: that name never got bars again, and after MAX_INFLIGHT such failures no name could start a fetch
(every setup walk then read "no bars" and refused with no_setup_window until a restart). Now a failed start frees the
slot, keeps the REFRESH_S wait (`_last_try`) and logs one warning a minute at most.

Plain runner: `python3 tests/test_round640_orb_bars_start_claim.py` (also collected by pytest). No network.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from entry_engine import orb_bars as OB  # noqa: E402


class _Boom:
    def __init__(self, *a, **k):
        pass

    def start(self):
        raise RuntimeError("can't start new thread")


class _Rig:
    def __init__(self, max_inflight=4, reserve=0):
        self.mi, self.rs = max_inflight, reserve

    def __enter__(self):
        OB._reset_for_tests()
        self.saved = (threading.Thread, OB.FETCH_ON, OB.MAX_INFLIGHT, OB.PRIORITY_RESERVE, OB._fetch)
        OB.FETCH_ON, OB.MAX_INFLIGHT, OB.PRIORITY_RESERVE = True, self.mi, self.rs
        self.log = []

        class _H(logging.Handler):
            def emit(h, record):
                self.log.append(record.getMessage())
        self.h = _H(level=logging.WARNING)
        # Round 643: a full run can leave logging disabled or the level raised by an earlier test; set what this rig needs.
        self.log_state = (OB.logger.level, OB.logger.disabled, logging.root.manager.disable)
        OB.logger.setLevel(logging.WARNING)
        OB.logger.disabled = False
        logging.disable(logging.NOTSET)
        OB.logger.addHandler(self.h)
        return self

    def __exit__(self, *a):
        threading.Thread, OB.FETCH_ON, OB.MAX_INFLIGHT, OB.PRIORITY_RESERVE, OB._fetch = self.saved
        OB.logger.removeHandler(self.h)
        OB.logger.setLevel(self.log_state[0])
        OB.logger.disabled = self.log_state[1]
        logging.disable(self.log_state[2])
        OB._reset_for_tests()


def test_failed_start_frees_the_slot_and_returns_none():
    with _Rig():
        threading.Thread = _Boom
        assert OB.get_bars("AAA", priority=True) is None
        assert len(OB._inflight) == 0


def test_repeated_failures_do_not_fill_every_slot():
    with _Rig(max_inflight=3):
        threading.Thread = _Boom
        for i in range(12):
            OB.get_bars(f"S{i}", now_mono=1000.0 + i, priority=True)
        assert len(OB._inflight) == 0


def test_refresh_wait_is_kept_then_the_next_call_starts_a_fetch():
    with _Rig():
        started = []

        class _Spy:
            def __init__(self, *a, **k):
                self.args = k.get("args")

            def start(self):
                started.append(self.args)

        threading.Thread = _Boom
        OB.get_bars("AAA", now_mono=1000.0, priority=True)
        threading.Thread = _Spy
        OB.get_bars("AAA", now_mono=1000.5, priority=True)               # inside REFRESH_S: nothing starts
        assert started == []
        OB.get_bars("AAA", now_mono=1000.0 + OB.REFRESH_S + 1, priority=True)
        assert len(started) == 1 and started[0][0][0] == "AAA" and len(OB._inflight) == 1


def test_one_warning_per_minute_not_one_per_name():
    with _Rig() as r:
        threading.Thread = _Boom
        for i in range(5):
            OB.get_bars(f"S{i}", now_mono=2000.0, priority=True)
        assert len(r.log) == 1 and "could not start a fetch thread" in r.log[0]


def test_normal_start_still_fetches_and_releases_the_slot():
    with _Rig():
        done = threading.Event()

        def fake(sym):
            done.set()
            return [{"date": "2026-10-26 09:15", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
        OB._fetch = fake
        assert OB.get_bars("AAA", priority=True) is None
        assert done.wait(2.0)
        t0 = time.monotonic()
        while OB._inflight and time.monotonic() - t0 < 2.0:
            time.sleep(0.01)
        assert len(OB._inflight) == 0


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
