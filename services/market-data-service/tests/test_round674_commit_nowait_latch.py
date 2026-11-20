"""Round 674 (D2c): Autonomous refuses the live_quotes ALTER SESSION commit settings for ADMIN with ORA-01031 (20 Nov VM: applied 0,
failed 12). That is permanent, so after the first refusal new connections skip the two statements. A transient error does not latch.

Run from services/market-data-service:   python3 -m pytest tests/test_round674_commit_nowait_latch.py -v
"""
from __future__ import annotations

import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import angelone_ws_feed as _f0  # noqa: F401


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    global f
    f = importlib.import_module("angelone_ws_feed")      # other test modules pop and re-import it
    monkeypatch.delenv("LIVE_QUOTES_COMMIT_NOWAIT", raising=False)
    f._LQ_SESSION.update({"applied": 0, "failed": 0, "last_error": None, "stopped": False})
    yield
    f._LQ_SESSION.update({"applied": 0, "failed": 0, "last_error": None, "stopped": False})


class _Cur:
    def __init__(self, log, err):
        self.log, self.err = log, err

    def execute(self, stmt, *a):
        self.log.append(stmt)
        if self.err:
            raise RuntimeError(self.err)

    def close(self):
        self.log.append("<close>")


class _Conn:
    def __init__(self, log, err=None):
        self.log, self.err = log, err

    def cursor(self):
        return _Cur(self.log, self.err)


_PRIV = "DatabaseError: ORA-01031: insufficient privileges"


def test_first_privilege_refusal_runs_one_statement_closes_and_latches():
    log = []
    f._lq_session_setup(_Conn(log, _PRIV), None)
    assert log == ["ALTER SESSION SET COMMIT_LOGGING = BATCH", "<close>"]     # the second statement is not tried
    assert f._LQ_SESSION["stopped"] is True and f._LQ_SESSION["failed"] == 1 and f._LQ_SESSION["applied"] == 0
    assert "ORA-01031" in f._LQ_SESSION["last_error"]


def test_later_connections_do_not_touch_the_database_after_the_latch():
    f._lq_session_setup(_Conn([], _PRIV), None)
    log = []
    f._lq_session_setup(_Conn(log, _PRIV), None)
    f._lq_session_setup(_Conn(log), None)
    assert log == [] and f._LQ_SESSION["failed"] == 1


def test_a_transient_error_does_not_latch_and_both_statements_are_still_tried():
    log = []
    f._lq_session_setup(_Conn(log, "ORA-02248: invalid option for ALTER SESSION"), None)
    assert log[:2] == ["ALTER SESSION SET COMMIT_LOGGING = BATCH", "ALTER SESSION SET COMMIT_WAIT = NOWAIT"]
    assert f._LQ_SESSION["stopped"] is False and f._LQ_SESSION["failed"] == 2


def test_success_path_unchanged_and_status_shows_the_latch():
    log = []
    f._lq_session_setup(_Conn(log), None)
    assert log == ["ALTER SESSION SET COMMIT_LOGGING = BATCH", "ALTER SESSION SET COMMIT_WAIT = NOWAIT", "<close>"]
    assert f._LQ_SESSION["applied"] == 1 and f._LQ_SESSION["stopped"] is False
