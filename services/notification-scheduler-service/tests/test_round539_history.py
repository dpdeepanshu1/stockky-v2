"""tests/test_round539_history.py - the notification history behind the Alerts feed (round 539, B1, E556).

Every /notify call is remembered (last 200, newest first), key-redacted and cut short; a storage problem never changes what /notify answers;
read-all moves the marker and deletes nothing.
Run from services/notification-scheduler-service:  python -m pytest tests/test_round539_history.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from notification import main as nm


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(nm, "_kv", None)
    monkeypatch.setattr(nm, "_history_mem", [])
    monkeypatch.setattr(nm, "_history_read_mem", 0.0)


def test_add_keeps_newest_first_and_caps_at_200():
    for i in range(205):
        nm._history_add(f"t{i}", "m", "all", "normal", True)
    items = nm.get_history(limit=500)["items"]
    assert len(items) == 200 and items[0]["title"] == "t204" and items[-1]["title"] == "t5"


def test_text_is_cut_and_a_token_shaped_secret_is_redacted():
    nm._history_add("x" * 400, "y" * 2000, "telegram", "high", False, "telegram: failed")
    e = nm.get_history()["items"][0]
    assert len(e["title"]) <= 160 and len(e["message"]) <= 600 and e["urgency"] == "high" and e["delivered"] is False and e["note"] == "telegram: failed"
    secret = "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    nm._history_add("t", f"token 123456789:{secret} end", "all", "normal", True)
    nm._history_add("t", f"https://api.telegram.org/bot123456789:{secret}/sendMessage", "all", "normal", True)
    for item in nm.get_history()["items"][:2]:
        assert secret not in item["message"]


def test_unread_counts_entries_newer_than_the_read_marker_and_read_all_deletes_nothing(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(nm.time, "time", lambda: t[0])
    nm._history_add("a", "m", "all", "normal", True)
    t[0] = 2000.0
    nm._history_add("b", "m", "all", "normal", True)
    assert nm.get_history()["unread"] == 2
    t[0] = 3000.0
    assert nm.history_read_all()["unread"] == 0
    h = nm.get_history()
    assert h["unread"] == 0 and h["count"] == 2 and h["read_at"] == 3000.0
    t[0] = 4000.0
    nm._history_add("c", "m", "all", "normal", True)
    assert nm.get_history()["unread"] == 1


def test_a_broken_store_never_breaks_history_add(monkeypatch):
    class Boom:
        def get(self, *a, **k): raise RuntimeError("db down")
        def set(self, *a, **k): raise RuntimeError("db down")
    monkeypatch.setattr(nm, "_kv", Boom())
    nm._history_add("t", "m", "all", "normal", True)      # must not raise
    assert nm.get_history()["count"] == 1                  # fell back to memory
    assert nm.history_read_all()["ok"] is True             # read marker too


def test_limit_is_clamped():
    for i in range(5):
        nm._history_add(f"t{i}", "m", "all", "normal", True)
    assert len(nm.get_history(limit=0)["items"]) == 5      # 0 means the default, never an empty or negative slice
    assert len(nm.get_history(limit=2)["items"]) == 2


def test_notify_records_both_a_delivery_and_a_failure(monkeypatch):
    monkeypatch.setattr(nm, "_outbox_enqueue", lambda *a, **k: "ob1")
    monkeypatch.setattr(nm, "_dispatch", lambda *a, **k: {"telegram": "sent"})
    r = nm.notify(nm.NotifyRequest(title="Picks", message="body", channel="all"))
    assert r["delivered"] is True
    monkeypatch.setattr(nm, "_dispatch", lambda *a, **k: {})
    r2 = nm.notify(nm.NotifyRequest(title="Lost", message="body2", channel="all"))
    assert r2["delivered"] is False
    items = nm.get_history()["items"]
    assert [i["title"] for i in items] == ["Lost", "Picks"] and [i["delivered"] for i in items] == [False, True]


def test_notify_answers_the_same_when_history_breaks(monkeypatch):
    monkeypatch.setattr(nm, "_outbox_enqueue", lambda *a, **k: "ob1")
    monkeypatch.setattr(nm, "_dispatch", lambda *a, **k: {"telegram": "sent"})
    monkeypatch.setattr(nm, "_hist_load", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    r = nm.notify(nm.NotifyRequest(title="Picks", message="body", channel="all"))
    assert r["delivered"] is True
