"""Round 639 (E665): the closed-market client gives its refresh claim back when the refresh thread cannot start.

`closed_for_day` sets `_cache["inflight"] = True` and then starts the `session-proof-refresh` thread; only that thread
(`_fetch`) clears the flag. If `Thread.start()` raised (for example "can't start new thread"), the outer `except`
swallowed it and `inflight` stayed True for good: no later call refreshed the verdict, so an unlisted holiday was never
recognised and the service traded on it until a restart. Now a failed start clears the claim, backs off one TTL and
logs one warning.

Plain functions (pytest runs them unchanged; `python3 <this file>` runs them without pytest). No network.
The module is loaded from its file under a unique name so the copy in THIS service is the one tested.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.normpath(os.path.join(HERE, "../session_proof_client.py"))


def _load():
    spec = importlib.util.spec_from_file_location("_spc_round639", PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_spc_round639"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Boom:
    def __init__(self, *a, **k):
        pass

    def start(self):
        raise RuntimeError("can't start new thread")


def _arm(spc):
    """Feature on, a market-data URL set, no pytest guard, clean state; returns what to restore."""
    saved = {k: os.environ.get(k) for k in ("MARKET_DATA_URL", "SESSION_PROOF_CLIENT_ENABLED", "PYTEST_CURRENT_TEST")}
    os.environ["MARKET_DATA_URL"] = "http://127.0.0.1:9"
    os.environ.pop("SESSION_PROOF_CLIENT_ENABLED", None)
    os.environ.pop("PYTEST_CURRENT_TEST", None)
    spc.reset_state()
    return saved


def _restore(spc, saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    spc.reset_state()


def test_failed_thread_start_frees_the_claim_and_does_not_raise():
    spc = _load()
    saved = _arm(spc)
    real = threading.Thread
    try:
        threading.Thread = _Boom
        assert spc.closed_for_day("2026-11-18") is False  # fail-open, no exception
    finally:
        threading.Thread = real
        state = dict(spc._cache)
        _restore(spc, saved)
    assert state["inflight"] is False


def test_failed_start_backs_off_one_ttl_then_the_next_call_retries():
    spc = _load()
    saved = _arm(spc)
    real = threading.Thread
    started = []

    class _Spy:
        def __init__(self, *a, **k):
            self.target = k.get("target")

        def start(self):
            started.append(self.target)

    try:
        threading.Thread = _Boom
        spc.closed_for_day("2026-11-18")
        threading.Thread = _Spy
        spc.closed_for_day("2026-11-18")           # inside the back-off: nothing starts
        assert started == []
        spc._cache["at"] = time.time() - spc._TTL_S - 1   # the TTL has passed
        spc.closed_for_day("2026-11-18")           # retry: a refresh thread is started
        assert len(started) == 1 and started[0] is spc._fetch
        assert spc._cache["inflight"] is True      # the (spy) thread owns the claim again
    finally:
        threading.Thread = real
        _restore(spc, saved)


def test_failed_start_logs_one_warning():
    import logging

    spc = _load()
    saved = _arm(spc)
    real = threading.Thread
    seen = []

    class _H(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    h = _H(level=logging.WARNING)
    log = logging.getLogger("_spc_round639")
    # Round 643: another test in a full run can leave logging switched off or the root level raised
    # (logging.disable, a dictConfig with disable_existing_loggers, a root level above WARNING). This test
    # needs the warning to reach its handler, so it sets that state itself and puts it back afterwards.
    saved_state = (log.level, log.disabled, logging.root.manager.disable)
    log.setLevel(logging.WARNING)
    log.disabled = False
    logging.disable(logging.NOTSET)
    log.addHandler(h)
    try:
        threading.Thread = _Boom
        spc.closed_for_day("2026-11-18")
        spc.closed_for_day("2026-11-18")           # back-off: no second attempt, no second warning
    finally:
        threading.Thread = real
        log.removeHandler(h)
        log.setLevel(saved_state[0])
        log.disabled = saved_state[1]
        logging.disable(saved_state[2])
        _restore(spc, saved)
    assert len(seen) == 1 and "could not start the refresh thread" in seen[0]


def test_normal_start_still_claims_and_the_fetch_releases_it():
    spc = _load()
    saved = _arm(spc)
    real = threading.Thread
    ran = threading.Event()
    try:
        spc._fetch = lambda: (spc.set_verdict("2026-11-18", True), ran.set())
        assert spc.closed_for_day("2026-11-18") is False   # first call returns the cached (empty) verdict
        assert ran.wait(2.0)
        time.sleep(0.05)
        assert spc._cache["inflight"] is False
        assert spc.closed_for_day("2026-11-18", refresh=False) is True
    finally:
        threading.Thread = real
        _restore(spc, saved)


def test_pytest_and_disabled_paths_still_never_claim():
    spc = _load()
    saved = _arm(spc)
    try:
        os.environ["PYTEST_CURRENT_TEST"] = "x"
        spc.closed_for_day("2026-11-18")
        assert spc._cache["inflight"] is False
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        os.environ["SESSION_PROOF_CLIENT_ENABLED"] = "false"
        assert spc.closed_for_day("2026-11-18") is False
        assert spc._cache["inflight"] is False
    finally:
        _restore(spc, saved)


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
