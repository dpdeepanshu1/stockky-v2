"""group 178: a failed /angelone/movers sweep (e.g. AngelOne login 403) is cached briefly instead of
re-logging in for every caller; the warning names the exception type."""
import logging
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as m  # noqa: E402

KEY = "angelone:movers"


class _Session:
    def __init__(self, exc):
        self.exc = exc
        self.logins = 0

    def is_configured(self):
        return True

    async def ensure_session(self):
        self.logins += 1
        raise self.exc


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.delenv("ANGELONE_MOVERS_ERROR_TTL_S", raising=False)
    store = {}
    monkeypatch.setattr(m, "_cache_set", lambda k, v, ttl=None: store.__setitem__(k, (v, ttl)) or None)
    # the store keeps (value, ttl); the handler gets the value back
    monkeypatch.setattr(m, "_cache_get", lambda k: (store.get(k) or (None, None))[0])
    monkeypatch.setattr(m, "_test_store", store, raising=False)
    sm = types.SimpleNamespace(get_all_symbols=lambda: {"AAA": "1", "BBB": "2"})
    monkeypatch.setitem(sys.modules, "angelone_scrip_master", sm)
    yield


def _use(monkeypatch, exc):
    sess = _Session(exc)
    mod = types.ModuleType("angelone_client")
    mod.get_session = lambda: sess
    monkeypatch.setitem(sys.modules, "angelone_client", mod)
    return sess


def test_error_is_cached_so_the_second_call_does_not_log_in_again(monkeypatch):
    sess = _use(monkeypatch, RuntimeError("login failed: 403"))
    first = m.angelone_movers()
    second = m.angelone_movers()
    assert first["status"] == "error" and second == first
    assert sess.logins == 1
    assert m._test_store[KEY][1] == 120


def test_empty_message_exception_names_its_type(monkeypatch, caplog):
    _use(monkeypatch, PermissionError())
    with caplog.at_level(logging.WARNING):
        out = m.angelone_movers()
    assert out["error"] == "PermissionError"
    assert any("angelone/movers failed: PermissionError" in r.getMessage() for r in caplog.records)


def test_message_is_kept_after_the_type(monkeypatch):
    _use(monkeypatch, RuntimeError("HTTP 403"))
    assert m.angelone_movers()["error"] == "RuntimeError: HTTP 403"


def test_zero_ttl_disables_the_error_cache(monkeypatch):
    monkeypatch.setenv("ANGELONE_MOVERS_ERROR_TTL_S", "0")
    sess = _use(monkeypatch, RuntimeError("x"))
    m.angelone_movers()
    m.angelone_movers()
    assert sess.logins == 2 and KEY not in m._test_store


@pytest.mark.parametrize("raw", ["", "  ", "abc", "-1"])
def test_blank_or_invalid_uses_default(monkeypatch, raw):
    monkeypatch.setenv("ANGELONE_MOVERS_ERROR_TTL_S", raw)
    assert m._movers_error_ttl_s() == 120.0


def test_custom_ttl(monkeypatch):
    monkeypatch.setenv("ANGELONE_MOVERS_ERROR_TTL_S", "30")
    _use(monkeypatch, RuntimeError("x"))
    m.angelone_movers()
    assert m._test_store[KEY][1] == 30


def test_not_configured_is_not_cached(monkeypatch):
    class _S:
        def is_configured(self):
            return False
    mod = types.ModuleType("angelone_client")
    mod.get_session = lambda: _S()
    monkeypatch.setitem(sys.modules, "angelone_client", mod)
    assert m.angelone_movers()["status"] == "not_configured"
    assert KEY not in m._test_store
