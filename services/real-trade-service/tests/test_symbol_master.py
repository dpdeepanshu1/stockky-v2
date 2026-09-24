"""
tests/test_symbol_master.py

100% coverage for symbol_master.py — closes all 45 missed lines:

  * line 50     : _clean() — strip .NS/.BO suffix, uppercase, strip whitespace
  * lines 57-96 : _load() — happy path (live fetch → new_symbols → persist
                  snapshot), zero-row warning, fetch exception, cache fallback
                  (both success and its own exception), and no-db path
  * lines 100-105: ensure_loaded() — fresh cache hit (early return) and the
                  stale/empty path that calls _load under the lock
  * lines 109-110: is_valid_symbol() — delegates to ensure_loaded + _clean
  * lines 118-119: get_all_symbols() — delegates to ensure_loaded, returns copy
  * line 123    : status() dict — loaded and unloaded states

Strategy:
  - Patch httpx.AsyncClient on the symbol_master module so no real network
    calls are made.
  - Reset module-level _symbols / _loaded_at between tests via monkeypatch.
  - Patch resilience.local_cache functions via the imported module object
    (works across all pytest versions).
  - Use a tiny SQLAlchemy in-memory db where needed.

Run from services/real-trade-service:
    python3 -m pytest tests/test_symbol_master.py -q \\
        --cov=symbol_master --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import AsyncMock, MagicMock

import models
import resilience.local_cache as _lc
import symbol_master as sm


def run(coro):
    return asyncio.run(coro)


# ── helpers ────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_globals(monkeypatch):
    """Reset module-level globals before every test to prevent cross-pollution."""
    monkeypatch.setattr(sm, "_symbols", set())
    monkeypatch.setattr(sm, "_loaded_at", 0.0)
    monkeypatch.setattr(sm, "_lock", asyncio.Lock())


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _make_scrip_row(symbol, exch="NSE"):
    return {"symbol": f"{symbol}-EQ", "exch_seg": exch}


def _mock_http_response(rows):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=rows)
    return resp


class _FakeAsyncClient:
    """Async context-manager that returns a mock httpx.AsyncClient."""
    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises

    async def __aenter__(self):
        if self._raises:
            raise self._raises
        client = AsyncMock()
        client.get = AsyncMock(return_value=self._response)
        return client

    async def __aexit__(self, *args):
        pass


# ── _clean ─────────────────────────────────────────────────────────────────────

class TestClean:
    def test_strips_ns_suffix(self):
        assert sm._clean("RELIANCE.NS") == "RELIANCE"

    def test_strips_bo_suffix(self):
        assert sm._clean("ITC.BO") == "ITC"

    def test_uppercases(self):
        assert sm._clean("tcs") == "TCS"

    def test_strips_whitespace(self):
        assert sm._clean("  SBIN  ") == "SBIN"

    def test_empty_string(self):
        assert sm._clean("") == ""

    def test_none_coerced_to_empty(self):
        assert sm._clean(None) == ""  # type: ignore[arg-type]


# ── _load ──────────────────────────────────────────────────────────────────────

class TestLoad:
    def test_happy_path_populates_symbols(self, monkeypatch):
        rows = [_make_scrip_row("RELIANCE"), _make_scrip_row("TCS"),
                {"symbol": "JUNK", "exch_seg": "BSE"}]
        monkeypatch.setattr(sm.httpx, "AsyncClient",
                            lambda **kw: _FakeAsyncClient(_mock_http_response(rows)))
        run(sm._load(db=None))
        assert "RELIANCE" in sm._symbols
        assert "TCS" in sm._symbols
        assert "JUNK" not in sm._symbols
        assert sm._loaded_at > 0

    def test_happy_path_persists_snapshot_when_db_given(self, db, monkeypatch):
        rows = [_make_scrip_row("INFY")]
        monkeypatch.setattr(sm.httpx, "AsyncClient",
                            lambda **kw: _FakeAsyncClient(_mock_http_response(rows)))
        saved = {}

        def _fake_save(db_, key, payload):
            saved["key"] = key
            saved["payload"] = payload

        monkeypatch.setattr(_lc, "save_snapshot", _fake_save)
        run(sm._load(db=db))
        assert saved.get("key") == "nse_symbol_master"
        assert "INFY" in saved["payload"]["symbols"]

    def test_zero_nse_eq_rows_warning_no_update(self, monkeypatch):
        rows = [{"symbol": "X-EQ", "exch_seg": "BSE"}]
        monkeypatch.setattr(sm.httpx, "AsyncClient",
                            lambda **kw: _FakeAsyncClient(_mock_http_response(rows)))
        run(sm._load(db=None))
        assert sm._symbols == set()

    def test_http_exception_is_caught(self, monkeypatch):
        monkeypatch.setattr(sm.httpx, "AsyncClient",
                            lambda **kw: _FakeAsyncClient(raises=ConnectionError("down")))
        run(sm._load(db=None))  # must not raise
        assert sm._symbols == set()

    def test_fetch_failure_falls_back_to_cache(self, db, monkeypatch):
        monkeypatch.setattr(sm.httpx, "AsyncClient",
                            lambda **kw: _FakeAsyncClient(raises=ConnectionError()))

        def _fake_load(db_, key):
            return {"symbols": ["HDFC", "ICICI"], "loaded_at": 1234567890.0}

        monkeypatch.setattr(_lc, "load_snapshot", _fake_load)
        run(sm._load(db=db))
        assert "HDFC" in sm._symbols
        assert sm._loaded_at == 1234567890.0

    def test_fetch_failure_empty_cache_snap_skipped(self, db, monkeypatch):
        monkeypatch.setattr(sm.httpx, "AsyncClient",
                            lambda **kw: _FakeAsyncClient(raises=ConnectionError()))

        monkeypatch.setattr(_lc, "load_snapshot", lambda db_, key: {})
        run(sm._load(db=db))
        assert sm._symbols == set()

    def test_fetch_failure_no_db_skips_cache(self, monkeypatch):
        monkeypatch.setattr(sm.httpx, "AsyncClient",
                            lambda **kw: _FakeAsyncClient(raises=ConnectionError()))
        run(sm._load(db=None))
        assert sm._symbols == set()

    def test_cache_fallback_exception_is_swallowed(self, db, monkeypatch):
        monkeypatch.setattr(sm.httpx, "AsyncClient",
                            lambda **kw: _FakeAsyncClient(raises=ConnectionError()))

        def _boom(db_, key):
            raise RuntimeError("cache read error")

        monkeypatch.setattr(_lc, "load_snapshot", _boom)
        run(sm._load(db=db))  # must not raise
        assert sm._symbols == set()

    def test_persist_snapshot_exception_is_swallowed(self, db, monkeypatch):
        rows = [_make_scrip_row("WIPRO")]
        monkeypatch.setattr(sm.httpx, "AsyncClient",
                            lambda **kw: _FakeAsyncClient(_mock_http_response(rows)))

        def _boom(db_, key, payload):
            raise RuntimeError("disk full")

        monkeypatch.setattr(_lc, "save_snapshot", _boom)
        run(sm._load(db=db))
        assert "WIPRO" in sm._symbols  # symbols set despite persist failure


# ── ensure_loaded ──────────────────────────────────────────────────────────────

class TestEnsureLoaded:
    def test_fresh_cache_skips_load(self, monkeypatch):
        monkeypatch.setattr(sm, "_symbols", {"RELIANCE"})
        monkeypatch.setattr(sm, "_loaded_at", time.time())
        load_called = []

        async def _fake_load(db=None):
            load_called.append(1)

        monkeypatch.setattr(sm, "_load", _fake_load)
        run(sm.ensure_loaded(db=None))
        assert load_called == []

    def test_empty_symbols_triggers_load(self, monkeypatch):
        load_called = []

        async def _fake_load(db=None):
            load_called.append(1)
            monkeypatch.setattr(sm, "_symbols", {"A"})

        monkeypatch.setattr(sm, "_load", _fake_load)
        run(sm.ensure_loaded(db=None))
        assert load_called == [1]

    def test_stale_cache_triggers_reload(self, monkeypatch):
        monkeypatch.setattr(sm, "_symbols", {"OLD"})
        monkeypatch.setattr(sm, "_loaded_at", 1.0)  # ancient timestamp
        load_called = []

        async def _fake_load(db=None):
            load_called.append(1)

        monkeypatch.setattr(sm, "_load", _fake_load)
        run(sm.ensure_loaded(db=None))
        assert load_called == [1]


# ── is_valid_symbol ────────────────────────────────────────────────────────────

class TestIsValidSymbol:
    def test_known_symbol_returns_true(self, monkeypatch):
        monkeypatch.setattr(sm, "_symbols", {"RELIANCE"})
        monkeypatch.setattr(sm, "_loaded_at", time.time())
        assert run(sm.is_valid_symbol("RELIANCE")) is True

    def test_unknown_symbol_returns_false(self, monkeypatch):
        monkeypatch.setattr(sm, "_symbols", {"RELIANCE"})
        monkeypatch.setattr(sm, "_loaded_at", time.time())
        assert run(sm.is_valid_symbol("NOTREAL")) is False

    def test_ns_suffix_cleaned_before_lookup(self, monkeypatch):
        monkeypatch.setattr(sm, "_symbols", {"TCS"})
        monkeypatch.setattr(sm, "_loaded_at", time.time())
        assert run(sm.is_valid_symbol("TCS.NS")) is True


# ── get_all_symbols ────────────────────────────────────────────────────────────

class TestGetAllSymbols:
    def test_returns_copy_of_symbol_set(self, monkeypatch):
        orig = {"SBIN", "HDFC"}
        monkeypatch.setattr(sm, "_symbols", orig)
        monkeypatch.setattr(sm, "_loaded_at", time.time())
        result = run(sm.get_all_symbols())
        assert result == orig
        result.add("MUTATED")
        assert "MUTATED" not in sm._symbols

    def test_empty_when_nothing_loaded(self, monkeypatch):
        async def _noop(db=None):
            pass

        monkeypatch.setattr(sm, "_load", _noop)
        result = run(sm.get_all_symbols())
        assert result == set()


# ── status ─────────────────────────────────────────────────────────────────────

class TestStatus:
    def test_status_when_loaded(self, monkeypatch):
        t = time.time()
        monkeypatch.setattr(sm, "_symbols", {"A", "B", "C"})
        monkeypatch.setattr(sm, "_loaded_at", t)
        s = sm.status()
        assert s["loaded_symbols"] == 3
        assert s["loaded_at"] == t
        assert s["age_seconds"] >= 0
        assert s["source_url"] == sm.SCRIP_MASTER_URL

    def test_status_when_not_loaded(self, monkeypatch):
        s = sm.status()
        assert s["loaded_symbols"] == 0
        assert s["loaded_at"] is None
        assert s["age_seconds"] is None
