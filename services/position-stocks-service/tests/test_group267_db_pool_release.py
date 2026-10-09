"""group 267: read-only Dhan routes hand the pooled DB connection back before the broker call, and a pool timeout
answers 503 (not an unhandled 500).

Why: POST /cycle/run failed with "QueuePool limit of size 2 overflow 2 reached" because /dhan/account and
/dhan/live-orders kept their connection (open transaction) while waiting on Dhan's HTTP API."""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from execution import dhan_client


class _Session:
    def __init__(self, events, new=(), dirty=(), deleted=(), rollback_raises=False):
        self.events, self.new, self.dirty, self.deleted = events, set(new), set(dirty), set(deleted)
        self.rollback_raises = rollback_raises

    def rollback(self):
        self.events.append("rollback")
        if self.rollback_raises:
            raise RuntimeError("boom")


class _Client:
    def __init__(self, events):
        self.events = events

    def get_fund_limits(self):
        self.events.append("dhan_call")
        return {"status": "success", "data": {"availabelBalance": 1000.0}}

    def get_super_order_list(self):
        self.events.append("dhan_call")
        return {"status": "success", "data": [{"orderId": "1"}]}

    def get_order_list(self):
        self.events.append("dhan_call")
        return {"status": "success", "data": [{"orderId": "2"}]}


@pytest.fixture()
def events(monkeypatch):
    ev = []

    def fake_client(db):
        ev.append("read_creds")
        return _Client(ev)

    monkeypatch.setattr(dhan_client, "_get_sdk_client", fake_client)
    return ev


def test_release_is_a_rollback_when_nothing_is_pending():
    ev = []
    dhan_client._release_connection(_Session(ev))
    assert ev == ["rollback"]


@pytest.mark.parametrize("kw", [{"new": [1]}, {"dirty": [1]}, {"deleted": [1]}])
def test_release_never_discards_pending_work(kw):
    ev = []
    dhan_client._release_connection(_Session(ev, **kw))
    assert ev == []


def test_release_never_raises():
    dhan_client._release_connection(_Session([], rollback_raises=True))
    dhan_client._release_connection(object())                      # no new/dirty/deleted at all


def test_get_funds_releases_between_credentials_and_the_dhan_call(events):
    out = dhan_client.get_funds(_Session(events), release_db=True)
    assert out == {"availabelBalance": 1000.0}
    assert events == ["read_creds", "rollback", "dhan_call"]


def test_super_and_plain_order_lists_release_too(events):
    assert dhan_client.get_super_order_list(_Session(events), release_db=True) == [{"orderId": "1"}]
    assert events == ["read_creds", "rollback", "dhan_call"]
    events.clear()
    assert dhan_client.get_order_list(_Session(events), release_db=True) == [{"orderId": "2"}]
    assert events == ["read_creds", "rollback", "dhan_call"]


def test_default_callers_keep_their_transaction(events):
    """The trading loop / reconcile call these with their own session and may hold uncommitted work: no rollback."""
    dhan_client.get_funds(_Session(events))
    dhan_client.get_super_order_list(_Session(events))
    dhan_client.get_order_list(_Session(events))
    assert "rollback" not in events


def test_pool_timeout_answers_503_with_retry_after():
    sqlalchemy_exc = pytest.importorskip("sqlalchemy.exc")
    import main

    class _Req:
        method = "POST"

        class url:
            path = "/cycle/run"

    exc = sqlalchemy_exc.TimeoutError("QueuePool limit of size 2 overflow 2 reached, connection timed out, timeout 30.00 "
                                      "(Background on this error at: https://sqlalche.me/e/21/3o7r)")
    resp = asyncio.run(main._db_pool_busy_handler(_Req(), exc))
    assert resp.status_code == 503 and resp.headers["retry-after"] == "5"
    assert "busy" in json.loads(resp.body)["detail"]
    assert main.app.exception_handlers.get(sqlalchemy_exc.TimeoutError) is main._db_pool_busy_handler
