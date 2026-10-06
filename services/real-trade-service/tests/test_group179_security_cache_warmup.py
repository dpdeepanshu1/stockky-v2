"""group 179: the Dhan security list is loaded in the background (boot + before TTL), not in the first order."""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from execution import dhan_client as dc  # noqa: E402


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(dc, "_security_cache", {})
    monkeypatch.setattr(dc, "_security_cache_loaded_at", 0.0)
    monkeypatch.setattr(dc, "_security_warm_task", None)
    monkeypatch.delenv("DHAN_SECURITY_WARM_ENABLED", raising=False)


def _creds(monkeypatch, value=("cid", "tok")):
    monkeypatch.setattr(dc.dhan_credentials, "get_decrypted_credentials", lambda db: value)


def _loader(monkeypatch, ok=True, calls=None):
    def load(db):
        if calls is not None:
            calls.append(1)
        if not ok:
            raise RuntimeError("download failed")
        dc._security_cache = {"TCS": "11536"}
        dc._security_cache_loaded_at = time.time()
    monkeypatch.setattr(dc, "_load_security_cache", load)


def test_no_credentials_does_not_load(monkeypatch):
    _creds(monkeypatch, None)
    calls = []
    _loader(monkeypatch, calls=calls)
    assert dc.warm_security_cache(object()) == "no_credentials" and calls == []


def test_credentials_check_error_is_treated_as_no_credentials(monkeypatch):
    def boom(db):
        raise RuntimeError("db down")
    monkeypatch.setattr(dc.dhan_credentials, "get_decrypted_credentials", boom)
    assert dc.warm_security_cache(object()) == "no_credentials"


def test_empty_cache_is_loaded(monkeypatch):
    _creds(monkeypatch)
    _loader(monkeypatch)
    assert dc.warm_security_cache(object()) == "loaded"
    assert dc.get_security_id(object(), "tcs") == "11536"       # order path now finds a warm cache


def test_fresh_cache_is_not_reloaded(monkeypatch):
    _creds(monkeypatch)
    calls = []
    _loader(monkeypatch, calls=calls)
    dc.warm_security_cache(object())
    assert dc.warm_security_cache(object()) == "fresh" and len(calls) == 1


def test_cache_older_than_refresh_age_is_reloaded_before_the_ttl(monkeypatch):
    _creds(monkeypatch)
    calls = []
    _loader(monkeypatch, calls=calls)
    dc.warm_security_cache(object())
    monkeypatch.setattr(dc, "_security_cache_loaded_at", time.time() - 19 * 3600)   # < 24 h TTL, > 18 h
    assert dc.warm_security_cache(object()) == "loaded" and len(calls) == 2


def test_failed_load_returns_failed_and_never_raises(monkeypatch):
    _creds(monkeypatch)
    _loader(monkeypatch, ok=False)
    assert dc.warm_security_cache(object()) == "failed"


def test_load_that_leaves_cache_empty_is_failed(monkeypatch):
    _creds(monkeypatch)
    monkeypatch.setattr(dc, "_load_security_cache", lambda db: None)   # "0 usable rows — keeping existing cache"
    assert dc.warm_security_cache(object()) == "failed"


def _run_loop_once(monkeypatch, results):
    """Drive keepwarm_security_cache through `results`, recording the sleeps it asks for."""
    sleeps = []
    it = iter(results)

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) > len(results):
            raise asyncio.CancelledError

    monkeypatch.setattr(dc.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(dc, "_warm_security_cache_own_session", lambda: next(it))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dc.keepwarm_security_cache())
    return sleeps


def test_loop_waits_by_outcome(monkeypatch):
    sleeps = _run_loop_once(monkeypatch, ["loaded", "failed", "no_credentials", "fresh"])
    assert sleeps[0] == dc._SECURITY_WARM_INITIAL_DELAY_S
    assert sleeps[1:5] == [dc._SECURITY_WARM_CHECK_EVERY_S, dc._SECURITY_WARM_RETRY_AFTER_FAILURE_S,
                           dc._SECURITY_WARM_RECHECK_NO_CREDENTIALS_S, dc._SECURITY_WARM_CHECK_EVERY_S]


def test_loop_survives_a_crashing_cycle(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) > 2:
            raise asyncio.CancelledError

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(dc.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(dc, "_warm_security_cache_own_session", boom)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dc.keepwarm_security_cache())
    assert sleeps[1] == dc._SECURITY_WARM_RETRY_AFTER_FAILURE_S


def test_start_task_is_single_and_switchable(monkeypatch):
    async def go():
        async def noop():
            await asyncio.sleep(0)
        monkeypatch.setattr(dc, "keepwarm_security_cache", noop)
        t1 = dc.start_security_warm_task()
        t2 = dc.start_security_warm_task()
        assert t1 is not None and t1 is t2
        await t1
        monkeypatch.setenv("DHAN_SECURITY_WARM_ENABLED", "0")
        assert dc.start_security_warm_task() is None
    asyncio.run(go())


@pytest.mark.parametrize("raw,expected", [("", True), ("1", True), ("0", False), ("off", False), (" No ", False)])
def test_switch_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("DHAN_SECURITY_WARM_ENABLED", raw)
    assert dc._security_warm_enabled() is expected
