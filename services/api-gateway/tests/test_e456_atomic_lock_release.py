"""E456 (round 435): `release_refresh_lock` frees the Redis lock with one atomic Lua compare-and-delete instead of GET then DELETE.

Run from services/api-gateway:
    python3 -m pytest tests/test_e456_atomic_lock_release.py -v
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import data_feed as df  # noqa: E402

KEY = df.LOCK_PREFIX + "TCS"


class _Base:
    def __init__(self):
        self.values = {}
        self.gets = 0
        self.deletes = []
        self.evals = []

    def set(self, key, val, **kw):
        self.values[key] = val
        return True

    def get(self, key):
        self.gets += 1
        return self.values.get(key)

    def delete(self, key):
        self.deletes.append(key)
        self.values.pop(key, None)

    def _run_script(self, script, key, token):
        self.evals.append((script, key, token))
        if self.values.get(key) == token:        # what the Lua script does, in one step
            self.values.pop(key, None)
            return 1
        return 0


class UpstashLike(_Base):
    def eval(self, script, keys=None, args=None):
        return self._run_script(script, keys[0], args[0])


class RedisPyLike(_Base):
    def eval(self, script, numkeys, *keys_and_args):
        assert numkeys == 1
        return self._run_script(script, keys_and_args[0], keys_and_args[1])


class NoEval(_Base):
    pass


class EvalRefused(_Base):
    def eval(self, *a, **k):
        raise RuntimeError("ERR scripting disabled")


@pytest.fixture(autouse=True)
def _clean():
    df._MEM_LOCKS.clear()
    df._MEM_LOCK_TOKENS.clear()
    yield
    df._MEM_LOCKS.clear()
    df._MEM_LOCK_TOKENS.clear()


@pytest.mark.parametrize("cls", [UpstashLike, RedisPyLike])
def test_e456_owner_release_uses_one_atomic_script_and_no_get_or_delete(cls):
    r = cls()
    tok = df.acquire_refresh_lock(r, "TCS")
    df.release_refresh_lock(r, "TCS", token=tok)
    assert len(r.evals) == 1 and "del" in r.evals[0][0] and r.evals[0][1:] == (KEY, tok)
    assert r.gets == 0 and r.deletes == []            # the non-atomic pair is not used
    assert KEY not in r.values


@pytest.mark.parametrize("cls", [UpstashLike, RedisPyLike])
def test_e456_a_late_release_leaves_the_newer_holders_lock(cls):
    r = cls()
    a = df.acquire_refresh_lock(r, "TCS")
    r.values.pop(KEY)                                 # the key expired in Redis
    df._MEM_LOCKS.clear()                             # ... and the process lock too
    b = df.acquire_refresh_lock(r, "TCS")
    assert r.values[KEY] == b
    df.release_refresh_lock(r, "TCS", token=a)        # A finishes late
    assert r.values[KEY] == b                         # B's lock is untouched
    df.release_refresh_lock(r, "TCS", token=b)
    assert KEY not in r.values


def test_e456_a_client_without_eval_keeps_the_get_delete_release():
    r = NoEval()
    tok = df.acquire_refresh_lock(r, "TCS")
    df.release_refresh_lock(r, "TCS", token=tok)
    assert r.deletes == [KEY] and KEY not in r.values


def test_e456_a_refused_script_falls_back_and_still_checks_the_owner():
    r = EvalRefused()
    a = df.acquire_refresh_lock(r, "TCS")
    r.values.pop(KEY)
    df._MEM_LOCKS.clear()
    b = df.acquire_refresh_lock(r, "TCS")
    df.release_refresh_lock(r, "TCS", token=a)
    assert r.values[KEY] == b and r.deletes == []     # fallback still refuses to free someone else's lock
    df.release_refresh_lock(r, "TCS", token=b)
    assert r.deletes == [KEY]


def test_e456_the_tokenless_release_stays_unconditional():
    import contextvars
    r = UpstashLike()
    r.values[KEY] = "written-by-old-code"
    contextvars.Context().run(df.release_refresh_lock, r, "TCS")
    assert r.deletes == [KEY] and r.evals == []       # no token anywhere: plain DELETE, as before


def test_e456_bytes_values_are_handled_by_the_fallback():
    class BytesRedis(NoEval):
        def get(self, key):
            v = super().get(key)
            return v.encode() if isinstance(v, str) else v
    r = BytesRedis()
    tok = df.acquire_refresh_lock(r, "TCS")
    df.release_refresh_lock(r, "TCS", token=tok)
    assert r.deletes == [KEY]
