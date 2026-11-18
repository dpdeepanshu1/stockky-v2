"""Round 637 (E663): dhan_data.start_background starts every part even when one cannot start, and frees its claim.

`start_background` set `_started = True` and then ran four steps in a row (scrip master load, live poller, websocket,
depth20). The first step that raised (for example "can't start new thread") skipped all the later ones, and because
`_started` stayed True nothing could finish the job: the Dhan live poller (which writes `live_quotes`) stayed off until
a restart, with only "dhan_data startup skipped" in the log.

Plain functions (pytest runs them unchanged; `python3 tests/test_round637_dhan_start_steps.py` runs them without
pytest). No network, no threads started.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dhan_data  # noqa: E402
from dhan_data import config, depth20, live_poller, scrip_master, ws_feed  # noqa: E402


class _Rig:
    """Replaces the config switches and the four start functions with recorders."""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def __enter__(self):
        self.saved = (config.enabled, config.live_poller_enabled, scrip_master.ensure_loaded, live_poller.start, ws_feed.start,
                      depth20.start, dhan_data._started)
        config.enabled = lambda: True
        config.live_poller_enabled = lambda: True

        def rec(name):
            def f(*a, **k):
                self.calls.append(name)
                if name in self.fail:
                    raise RuntimeError("can't start new thread")
            return f

        scrip_master.ensure_loaded = rec("scrip")
        live_poller.start = rec("poller")
        ws_feed.start = rec("ws")
        depth20.start = rec("depth")
        dhan_data._started = False
        return self

    def __exit__(self, *a):
        (config.enabled, config.live_poller_enabled, scrip_master.ensure_loaded, live_poller.start, ws_feed.start,
         depth20.start, dhan_data._started) = self.saved


def test_a_failing_first_step_does_not_skip_the_others():
    with _Rig(fail={"scrip"}) as r:
        dhan_data.start_background(lambda: [])           # must not raise
        assert r.calls == ["scrip", "poller", "ws", "depth"], r.calls


def test_a_failing_poller_still_starts_websocket_and_depth20():
    with _Rig(fail={"poller"}) as r:
        dhan_data.start_background(lambda: [])
        assert r.calls == ["scrip", "poller", "ws", "depth"], r.calls


def test_the_claim_is_freed_after_a_failure_so_a_later_call_retries():
    with _Rig(fail={"poller"}) as r:
        dhan_data.start_background(lambda: [])
        assert dhan_data._started is False
        r.fail.clear()
        r.calls.clear()
        dhan_data.start_background(lambda: [])
        assert r.calls == ["scrip", "poller", "ws", "depth"], r.calls
        assert dhan_data._started is True


def test_a_clean_start_runs_each_step_once_and_stays_claimed():
    with _Rig() as r:
        dhan_data.start_background(lambda: [])
        dhan_data.start_background(lambda: [])           # idempotent
        assert r.calls == ["scrip", "poller", "ws", "depth"], r.calls
        assert dhan_data._started is True


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(n, "passed")
