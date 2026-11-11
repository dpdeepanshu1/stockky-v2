"""E455 (round 434): price-alert add / delete / evaluate are read-change-write on one stored list; they now run under one lock.

Each test uses an in-memory kv whose read sleeps a little, so two unlocked callers would reliably read the same list. Run from services/api-gateway:
    python3 -m pytest tests/test_e455_price_alert_lock.py -v
"""
import os
import sys
import threading
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import data_feed as df  # noqa: E402
import kv_cache  # noqa: E402


@pytest.fixture
def kv(monkeypatch):
    store = {}

    def slow_get(key, *a, **k):
        value = store.get(key)
        time.sleep(0.02)          # widen the read-to-write window: an unlocked second caller reads the same list here
        return value

    def set_(key, value, *a, **k):
        store[key] = value
        return True

    monkeypatch.setattr(kv_cache, "kv_get", slow_get)
    monkeypatch.setattr(kv_cache, "kv_set", set_)
    return store


def _ids(store):
    return sorted(a["id"] for a in (store.get(df.PRICE_ALERTS_KEY) or {}).get("alerts", []))


def test_e455_simultaneous_adds_are_all_kept(kv):
    threads = [threading.Thread(target=df.add_price_alert, args=(f"S{i}", 100 + i)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stored = kv[df.PRICE_ALERTS_KEY]["alerts"]
    assert sorted(a["symbol"] for a in stored) == sorted(f"S{i}" for i in range(8))


def test_e455_a_delete_and_an_add_at_the_same_time_both_land(kv):
    first = df.add_price_alert("AAA", 10)
    out = {}
    t1 = threading.Thread(target=lambda: out.update(deleted=df.delete_price_alert(first["id"])))
    t2 = threading.Thread(target=lambda: out.update(added=df.add_price_alert("BBB", 20)))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert out["deleted"] is True
    assert [a["symbol"] for a in kv[df.PRICE_ALERTS_KEY]["alerts"]] == ["BBB"]


def test_e455_an_add_during_the_evaluate_price_lookup_is_not_lost(kv, monkeypatch):
    df.add_price_alert("TCS", 100)
    added = {}

    def cache_with_side_effect():
        # runs inside evaluate's price lookup, i.e. after its first read and before its save
        added["entry"] = df.add_price_alert("INFY", 50)
        return {"quotes": {"TCS": {"price": 150}}}

    monkeypatch.setattr(df, "get_bulk_quote_cache", cache_with_side_effect)
    triggered = df.evaluate_price_alerts()
    assert [t["symbol"] for t in triggered] == ["TCS"]
    stored = {a["symbol"]: a for a in kv[df.PRICE_ALERTS_KEY]["alerts"]}
    assert set(stored) == {"TCS", "INFY"}                     # the alert added mid-evaluate survived the evaluate's save
    assert stored["TCS"]["trigger_count"] == 1 and stored["INFY"]["trigger_count"] == 0


def test_e455_a_delete_during_the_evaluate_price_lookup_stays_deleted(kv, monkeypatch):
    keep = df.add_price_alert("TCS", 100)
    gone = df.add_price_alert("INFY", 50)

    def cache_with_side_effect():
        assert df.delete_price_alert(gone["id"]) is True
        return {"quotes": {"TCS": {"price": 150}, "INFY": {"price": 60}}}

    monkeypatch.setattr(df, "get_bulk_quote_cache", cache_with_side_effect)
    triggered = df.evaluate_price_alerts()
    assert [t["id"] for t in triggered] == [keep["id"]]       # the deleted alert neither fires nor comes back
    assert _ids(kv) == [keep["id"]]


def test_e455_evaluate_keeps_its_cooldown_and_trigger_fields(kv, monkeypatch):
    df.add_price_alert("TCS", 100)
    monkeypatch.setattr(df, "get_bulk_quote_cache", lambda: {"quotes": {"TCS": {"price": 150}}})
    assert len(df.evaluate_price_alerts()) == 1
    assert df.evaluate_price_alerts() == []                   # 15 minute cooldown still applies
    assert kv[df.PRICE_ALERTS_KEY]["alerts"][0]["trigger_count"] == 1


def test_e455_the_lock_is_released_when_a_call_raises(kv, monkeypatch):
    monkeypatch.setattr(df, "list_price_alerts", lambda: (_ for _ in ()).throw(RuntimeError("kv down")))
    with pytest.raises(RuntimeError):
        df.add_price_alert("TCS", 10)
    with pytest.raises(RuntimeError):
        df.delete_price_alert("x")
    assert not df._PRICE_ALERTS_LOCK.locked()
