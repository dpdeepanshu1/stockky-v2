"""E382 / E383: /sector-strength fetches history off the event loop and disposes every SQLAlchemy engine it creates."""
from __future__ import annotations

import asyncio
import sys
import threading
import types

import numpy as np
import pandas as pd
import pytest

from ._tech_loader import load


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        rows = self._rows

        class _R:
            def fetchall(self_):
                return rows

            def fetchone(self_):
                return rows[0] if rows else None
        return _R()


def _frame(step):
    idx = pd.bdate_range("2026-08-01", periods=40)
    return pd.DataFrame({"Close": 100 + step * np.arange(40.0)}, index=idx)


def test_sector_strength_blocking_fetch_off_loop_and_engines_disposed(monkeypatch):
    tm = load("technical_main_e382")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    disposed, threads = [], []
    peers = [(f"P{i}",) for i in range(9)]

    class _Engine:
        def connect(self):
            return _Conn(peers)

        def dispose(self):
            disposed.append(1)

    fake = types.ModuleType("sqlalchemy")
    fake.create_engine = lambda *a, **k: _Engine()
    fake.text = lambda q: q
    monkeypatch.setitem(sys.modules, "sqlalchemy", fake)

    def fetch(sym, force=False):
        threads.append(threading.get_ident())
        return _frame(1.0 if sym == "AAA" else 0.1)

    monkeypatch.setattr(tm, "_fetch_history", fetch)

    async def go():
        main_thread = threading.get_ident()
        out = await tm.sector_relative_strength("AAA", sector="IT")
        return main_thread, out

    main_thread, out = asyncio.run(go())
    assert out["symbol"] == "AAA" and out.get("peers_in_window") == 9 and out["passes"] is True
    assert threads and all(t != main_thread for t in threads)      # E382: never on the event-loop thread
    assert len(disposed) == 1                                      # E383: the peers engine


def test_sector_lookup_engine_disposed_too(monkeypatch):
    tm = load("technical_main_e383")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    disposed = []

    class _Engine:
        def connect(self):
            return _Conn([("IT",)])

        def dispose(self):
            disposed.append(1)

    fake = types.ModuleType("sqlalchemy")
    fake.create_engine = lambda *a, **k: _Engine()
    fake.text = lambda q: q
    monkeypatch.setitem(sys.modules, "sqlalchemy", fake)
    monkeypatch.setattr(tm, "_fetch_history", lambda s, force=False: None)
    out = asyncio.run(tm.sector_relative_strength("AAA"))
    assert out["sector"] == "IT" and len(disposed) == 1           # the sector lookup engine (no history, so the peers query never runs)
