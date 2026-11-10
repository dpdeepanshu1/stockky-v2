"""Round 313: Store.events() keeps the row's own id / ts / kind / underlying even if a payload has such keys, and survives an unreadable row; kv_get survives a damaged value."""
from __future__ import annotations

import pytest

from fno import store as st


@pytest.fixture
def db():
    s = st.Store(":memory:")
    yield s
    s.close()


def test_payload_keys_cannot_overwrite_row_columns(db):
    db.add_event("manual", {"id": 999, "ts": 1.0, "kind": "fake", "underlying": "X", "msg": "hi"}, underlying="NIFTY", ts=1700000000.0)
    e = db.events()[0]
    assert e["id"] != 999 and e["ts"] == 1700000000.0 and e["kind"] == "manual" and e["underlying"] == "NIFTY" and e["msg"] == "hi"


def test_unreadable_event_row_does_not_break_the_list(db):
    db.add_event("a", {"x": 1})
    db._db.execute("INSERT INTO events(ts, kind, underlying, data) VALUES (1.0, 'bad', '', 'not json')")
    db._db.commit()
    kinds = {e["kind"]: e for e in db.events()}
    assert kinds["a"]["x"] == 1 and kinds["bad"]["data_unreadable"] is True


def test_damaged_kv_reads_as_default(db):
    db._db.execute("INSERT INTO kv(key, value, updated) VALUES ('fno_armed_day', '{broken', 1.0)")
    db._db.commit()
    assert db.kv_get("fno_armed_day") is None
    assert db.kv_get("fno_armed_day", "d") == "d"
    db.kv_set("k", {"a": 1})
    assert db.kv_get("k") == {"a": 1}
