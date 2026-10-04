"""tests/test_surprise_last_result_expiry_real_kv.py - group 132.

Group 131 restored an expired last-result row with kv_cache.get_stale, but main.py's closed-market warm
called the PLAIN loader first, and kv_cache's plain read DELETES a row it finds expired, so the stale read
that followed found nothing and the full quote sweep still ran. The group 120/131 tests used fake loaders
and fake kv stores, which cannot show that. These tests run the REAL kv_cache against a real SQL table
(SQLite with datetime parsing, so expires_at comes back as a datetime like on Oracle/Postgres).

Run from services/api-gateway:
    python3 -m pytest tests/test_surprise_last_result_expiry_real_kv.py -v
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time

import pytest
from sqlalchemy import create_engine, event, text

import kv_cache as kc
import surprise_scanner as sc

KEY = sc.SURPRISE_LAST_RESULT_CACHE_KEY


def _utcstamp(raw: bytes):
    v = dt.datetime.fromisoformat(raw.decode())
    return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)


# A uniquely named column type, so this converter cannot affect any other sqlite user in the suite.
sqlite3.register_converter("UTCSTAMP", _utcstamp)


@pytest.fixture
def db(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path / 'kv.db'}", connect_args={"detect_types": sqlite3.PARSE_DECLTYPES})

    @event.listens_for(eng, "connect")
    def _now(dbapi_conn, _rec):          # kv_cache's Postgres upsert uses NOW()
        dbapi_conn.create_function("NOW", 0, lambda: dt.datetime.now(dt.timezone.utc).isoformat(sep=" "))

    with eng.begin() as c:
        c.execute(text("CREATE TABLE stockky_kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, "
                       "expires_at UTCSTAMP NULL, updated_at TEXT)"))
    monkeypatch.setattr(kc, "_neon_engine", eng)
    monkeypatch.setattr(kc, "_neon_init", True)
    kc._mem.delete(KEY)
    yield eng
    kc._mem.delete(KEY)
    eng.dispose()


def _insert(eng, *, age_sec, expired_sec_ago):
    exp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=expired_sec_ago)).isoformat(sep=" ")
    payload = {"result": {"count": 3, "stocks": []}, "scan_ts": time.time() - age_sec}
    with eng.begin() as c:
        c.execute(text("INSERT INTO stockky_kv (k, v, expires_at) VALUES (:k, :v, :e)"),
                  {"k": KEY, "v": json.dumps(payload), "e": exp})


def _rows(eng):
    with eng.connect() as c:
        return c.execute(text("SELECT count(*) FROM stockky_kv WHERE k = :k"), {"k": KEY}).scalar()


def _engine():
    return sc.SurpriseStockEngine()


def test_precondition_the_key_is_durable():
    assert kc._is_durable(KEY)


def test_the_plain_read_deletes_an_expired_row_which_is_why_the_order_matters(db):
    _insert(db, age_sec=3600, expired_sec_ago=3200)
    e = _engine()
    e._load_last_result_from_durable_cache()
    assert e._last_result is None
    assert _rows(db) == 0                      # the plain read removed the only copy


def test_plain_then_stale_loses_the_row_group131_order(db):
    _insert(db, age_sec=3600, expired_sec_ago=3200)
    e = _engine()
    e._load_last_result_from_durable_cache()
    e._load_last_result_stale_from_durable_cache()
    assert e._last_result is None              # what group 131 shipped could never restore


def test_stale_first_restores_an_expired_row_and_keeps_it(db):
    _insert(db, age_sec=3600, expired_sec_ago=3200)
    e = _engine()
    e._load_last_result_stale_from_durable_cache()
    assert e._last_result == {"count": 3, "stocks": []}
    assert (time.time() - e._last_scan_ts) > sc.SURPRISE_CACHE_MAX_AGE_SEC   # never served as fresh
    assert _rows(db) == 1


def test_the_saved_row_now_outlives_a_weekend(db):
    """Save through the real scan() persistence call and check the stored expiry."""
    ttl = sc._last_result_durable_ttl_sec(sc.SURPRISE_CACHE_MAX_AGE_SEC)
    kc.set(KEY, {"result": {"count": 1}, "scan_ts": time.time()}, ttl)
    with db.connect() as c:
        exp = c.execute(text("SELECT expires_at FROM stockky_kv WHERE k = :k"), {"k": KEY}).scalar()
    remaining = (exp - dt.datetime.now(dt.timezone.utc)).total_seconds()
    assert remaining > 3 * 24 * 3600


def test_a_plain_read_three_days_later_restores_it_instead_of_purging(db):
    """Simulate three days passing by moving the stored expiry back; the plain loader (used by
    scan(cached=True)) must still find the row, and scan() then age-rejects it."""
    kc.set(KEY, {"result": {"count": 2}, "scan_ts": time.time() - 3 * 24 * 3600},
           sc._last_result_durable_ttl_sec(sc.SURPRISE_CACHE_MAX_AGE_SEC))
    with db.begin() as c:
        old = c.execute(text("SELECT expires_at FROM stockky_kv WHERE k = :k"), {"k": KEY}).scalar()
        c.execute(text("UPDATE stockky_kv SET expires_at = :e WHERE k = :k"),
                  {"e": (old - dt.timedelta(days=3)).isoformat(sep=" "), "k": KEY})
    kc._mem.delete(KEY)                        # fresh process: memory empty
    e = _engine()
    e._load_last_result_from_durable_cache()
    assert e._last_result == {"count": 2}
    assert (time.time() - e._last_scan_ts) > sc.SURPRISE_CACHE_MAX_AGE_SEC
    assert _rows(db) == 1
