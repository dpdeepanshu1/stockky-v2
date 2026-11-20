"""Round 670 (E692, D2c): the dedicated live_quotes writer connections commit without waiting for the redo write (Oracle).

Run from services/market-data-service:   python3 -m pytest tests/test_round670_live_quotes_commit_nowait.py -v
"""
from __future__ import annotations

import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import angelone_ws_feed as _f0
import kv_cache as kv


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    global f
    f = importlib.import_module("angelone_ws_feed")      # other test modules pop and re-import it
    monkeypatch.delenv("LIVE_QUOTES_COMMIT_NOWAIT", raising=False)
    f._LQ_SESSION.update({"applied": 0, "failed": 0, "last_error": None})
    yield


class _Cur:
    def __init__(self, log, fail_on=(), cur_fail=False):
        self.log, self.fail_on = log, set(fail_on)

    def execute(self, stmt, *a):
        self.log.append(stmt)
        if any(k in stmt for k in self.fail_on):
            raise RuntimeError("ORA-02248: invalid option for ALTER SESSION")

    def close(self):
        self.log.append("<close>")


class _Conn:
    def __init__(self, log, fail_on=(), no_cursor=False):
        self.log, self.fail_on, self.no_cursor = log, fail_on, no_cursor

    def cursor(self):
        if self.no_cursor:
            raise RuntimeError("closed")
        return _Cur(self.log, self.fail_on)


class TestSessionHook:
    def test_both_statements_run_and_the_cursor_is_closed(self):
        log = []
        f._lq_session_setup(_Conn(log), None)
        assert log == ["ALTER SESSION SET COMMIT_LOGGING = BATCH", "ALTER SESSION SET COMMIT_WAIT = NOWAIT", "<close>"]
        assert f._LQ_SESSION["applied"] == 1 and f._LQ_SESSION["failed"] == 0

    def test_a_refused_statement_does_not_stop_the_other_or_raise(self):
        log = []
        f._lq_session_setup(_Conn(log, fail_on=("COMMIT_LOGGING",)), None)
        assert "ALTER SESSION SET COMMIT_WAIT = NOWAIT" in log and log[-1] == "<close>"
        assert f._LQ_SESSION["failed"] == 1 and f._LQ_SESSION["applied"] == 0
        assert "ORA-02248" in f._LQ_SESSION["last_error"]

    def test_both_refused_is_counted_and_still_does_not_raise(self):
        f._lq_session_setup(_Conn([], fail_on=("ALTER",)), None)
        assert f._LQ_SESSION["failed"] == 2

    def test_no_cursor_does_not_raise(self):
        f._lq_session_setup(_Conn([], no_cursor=True), None)
        assert f._LQ_SESSION["failed"] == 1

    def test_switch(self, monkeypatch):
        assert f.live_quotes_commit_nowait() is True
        for v in ("0", "false", "off", "no"):
            monkeypatch.setenv("LIVE_QUOTES_COMMIT_NOWAIT", v)
            assert f.live_quotes_commit_nowait() is False
        monkeypatch.setenv("LIVE_QUOTES_COMMIT_NOWAIT", "  ")
        assert f.live_quotes_commit_nowait() is True


class TestEngineHook:
    def _build(self, monkeypatch, eng):
        class _OC:
            @staticmethod
            def build_oracle_engine(url, **kw):
                return eng, url
        monkeypatch.setattr(kv, "_neon_url", lambda: "oracle+oracledb://u:p@h/db")
        monkeypatch.setattr(kv, "_oc", _OC, raising=False)
        return f._build_live_quotes_engine("oracle")

    def test_hook_is_attached_to_the_dedicated_engine_and_a_refusing_database_still_connects(self, monkeypatch):
        from sqlalchemy import create_engine, text
        eng = create_engine("sqlite://")             # ALTER SESSION is a syntax error here: the hook must swallow it
        assert self._build(monkeypatch, eng) is eng
        with eng.connect() as c:
            assert c.execute(text("select 1")).scalar() == 1
        assert f._LQ_SESSION["failed"] == 2

    def test_hook_not_attached_when_switched_off(self, monkeypatch):
        from sqlalchemy import create_engine, text
        monkeypatch.setenv("LIVE_QUOTES_COMMIT_NOWAIT", "0")
        eng = create_engine("sqlite://")
        self._build(monkeypatch, eng)
        with eng.connect() as c:
            c.execute(text("select 1"))
        assert f._LQ_SESSION["failed"] == 0 and f._LQ_SESSION["applied"] == 0

    def test_postgres_engine_gets_no_oracle_statements(self, monkeypatch):
        import sqlalchemy
        from sqlalchemy import create_engine, event
        sqlite = create_engine("sqlite://")        # stands in for the Postgres engine (no psycopg in every sandbox)
        monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **k: sqlite)
        monkeypatch.setattr(kv, "_neon_url", lambda: "postgresql://u:p@h/db")
        built = f._build_live_quotes_engine("postgresql")
        assert built is sqlite
        assert not event.contains(built, "connect", f._lq_session_setup)

    def test_a_stub_engine_that_cannot_take_events_is_tolerated(self, monkeypatch):
        class _Stub:
            pool = None
        assert self._build(monkeypatch, _Stub()).__class__ is _Stub

    def test_status_reports_the_setting(self):
        st = f.live_quotes_engine_status()
        assert st["commit_nowait"]["enabled"] is True and "applied" in st["commit_nowait"]
