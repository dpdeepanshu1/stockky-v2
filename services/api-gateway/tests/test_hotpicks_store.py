"""tests/test_hotpicks_store.py — coverage for api-gateway/hotpicks_store.py

The Hot Picks durable store: stop flag, row (de)serialisation helpers, the best-effort writer
(Postgres executemany vs Oracle row-by-row, prune), the readers (payload rebuild, freshness), the
memoised feed-health audit and the two repair passes (price via market-data, scores via decision).

No network, no database, no real sqlalchemy / httpx. Every test loads a FRESH copy of the module (so
_AUDIT_CACHE and the import-time env floats never leak between tests) with `hotpicks_schema`,
`sqlalchemy` and `httpx` replaced by small fakes in sys.modules. The fake engine records every SQL
statement and bind and answers by statement shape; `time` inside the module is replaced by a fake
clock so the audit TTL is deterministic and the repair loops never really sleep.

A separate class drives the store through the REAL hotpicks_schema (payload -> rows -> adapt_rows ->
stored row -> item) and guards against drift: every `hp.<name>` the store calls must exist in the
schema, `_payload_rows` must emit exactly ROW_KEYS, and every column the store reads or updates must
exist in SELECT_COLUMNS.

Run from services/api-gateway:
    python3 -m pytest tests/test_hotpicks_store.py -v
"""
from __future__ import annotations

import ast
import importlib.util
import json
import logging
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
_STORE_PATH = os.path.join(_SERVICE, "hotpicks_store.py")
_SCHEMA_PATH = os.path.join(_SERVICE, "hotpicks_schema.py")

_ENV_KEYS = (
    "HOTPICKS_DB_FRESH_HOURS", "HOTPICKS_RETENTION_HOURS", "HOTPICKS_TABLE_HOURS",
    "HOTPICKS_AUDIT_TTL_SEC", "MAX_STOCK_PRICE", "MARKET_DATA_URL", "DECISION_URL",
    "CACHE_DATABASE_URL", "DATABASE_URL", "TRAINING_DATABASE_URL", "ORACLE_DSN",
)

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_now():
    """Re-anchor NOW to the real clock at the start of every test.

    The module under test reads the real clock (datetime.now) while NOW used to be frozen at import time,
    so tight age assertions (abs=0.05h = 3 min) flaked whenever a long full-suite run executed a test
    more than ~3 minutes after collection."""
    globals()["NOW"] = datetime.now(timezone.utc)
    yield


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeText:
    def __init__(self, sql):
        self.sql = sql

    def __str__(self):
        return self.sql


class FakeResult:
    def __init__(self, keys=(), rows=(), scalar=None, fetchone=None):
        self._keys = list(keys)
        self._rows = list(rows)
        self._scalar = scalar
        self._fetchone = fetchone

    def keys(self):
        return self._keys

    def fetchall(self):
        return list(self._rows)

    def scalar(self):
        return self._scalar

    def fetchone(self):
        return self._fetchone


class FakeConn:
    def __init__(self, eng):
        self.eng = eng

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.eng.calls.append((sql, params))
        for needle, exc in self.eng.raises_on:
            if needle in sql:
                raise exc
        e = self.eng
        if sql in e.answers:
            return e.answers[sql]
        if sql.startswith("EXISTS"):
            return FakeResult(scalar=e.exists)
        if sql.startswith("SELECT_RECENT"):
            return FakeResult(keys=e.recent_keys, rows=e.recent_rows)
        if "COUNT(*)" in sql:
            return FakeResult(scalar=e.count)
        if "MAX(updated_at)" in sql:
            return FakeResult(fetchone=e.max_row)
        if sql.startswith("SELECT symbol, section, item_json"):
            return FakeResult(rows=e.target_rows)
        return FakeResult()


class _Ctx:
    def __init__(self, eng, kind):
        self.eng = eng
        self.kind = kind

    def __enter__(self):
        if self.kind == "begin":
            self.eng.begins += 1
        else:
            self.eng.connects += 1
        return FakeConn(self.eng)

    def __exit__(self, *a):
        return False


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.begins = 0
        self.connects = 0
        self.disposed = 0
        self.dispose_raises = False
        self.raises_on = []          # [(substring, exception)]
        self.answers = {}            # exact SQL -> FakeResult (wins over shape matching)
        self.exists = True
        self.count = 0
        self.recent_keys = []
        self.recent_rows = []
        self.max_row = None
        self.target_rows = []

    def begin(self):
        return _Ctx(self, "begin")

    def connect(self):
        return _Ctx(self, "connect")

    def dispose(self):
        self.disposed += 1
        if self.dispose_raises:
            raise RuntimeError("dispose boom")

    def sqls(self):
        return [c[0] for c in self.calls]

    def find(self, prefix):
        return [c for c in self.calls if c[0].startswith(prefix)]


class FakeHP:
    """Stand-in for hotpicks_schema with deterministic, self-describing SQL strings."""

    TABLE_NAME = "hotpicks_static_feed"
    ROW_KEYS = ("symbol", "section", "decision", "score", "news_score", "headline_count",
                "signal_strength", "from_scan", "next_earnings_date", "summary", "item_json",
                "generated_at", "extra_key")

    def __init__(self):
        self.url = "postgresql://fake"
        self.dial = "postgresql"
        self.writer = FakeEngine()
        self.shared = FakeEngine()
        self.writer_none = False
        self.shared_none = False
        self.made = []
        self.shared_apps = []
        self.adapted = []
        self.ensure_result = {"ok": True}
        self.ensure_raises = None

    def database_url(self):
        return self.url

    def dialect(self):
        return self.dial

    def make_engine(self, app="x"):
        self.made.append(app)
        return None if self.writer_none else self.writer

    def shared_engine(self, app="x"):
        self.shared_apps.append(app)
        return None if self.shared_none else self.shared

    def adapt_rows(self, rows, dial=None):
        self.adapted.append(dial)
        return [dict(r, adapted_for=dial) for r in rows]

    def upsert_sql(self, dial=None):
        return f"UPSERT[{dial}]"

    def table_exists_sql(self, dial=None):
        return f"EXISTS[{dial}]"

    def select_recent_sql(self, dial=None):
        return f"SELECT_RECENT[{dial}]"

    def delete_older_than_sql(self, dial=None):
        return f"DELETE_OLD[{dial}]"

    def now_func(self, dial=None):
        return f"NOW[{dial}]"

    def coerce_bool(self, v):
        return bool(v)

    def ensure_hotpicks_schema(self):
        if self.ensure_raises is not None:
            raise self.ensure_raises
        return self.ensure_result


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeHttp:
    """Stand-in for the httpx module: Client(...).get(url) answered from `routes`."""

    def __init__(self):
        self.routes = {}
        self.gets = []
        self.client_kwargs = []

    def module(self):
        http = self
        m = types.ModuleType("httpx")

        class Client:
            def __init__(self, **kw):
                http.client_kwargs.append(kw)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url):
                http.gets.append(url)
                r = http.routes.get(url)
                if isinstance(r, Exception):
                    raise r
                return r if r is not None else FakeResp(404, {})

        m.Client = Client
        return m


def _fake_sqlalchemy():
    m = types.ModuleType("sqlalchemy")
    m.text = FakeText
    return m


class Env:
    def __init__(self, monkeypatch):
        self.mp = monkeypatch
        self.hp = FakeHP()
        self.clock = FakeClock()
        self.http = FakeHttp()
        self.n = 0

    def load(self, no_schema=False, no_sa=False, **env):
        for k, v in env.items():
            self.mp.setenv(k, v)
        self.mp.setitem(sys.modules, "hotpicks_schema", None if no_schema else self.hp)
        self.mp.setitem(sys.modules, "sqlalchemy", None if no_sa else _fake_sqlalchemy())
        self.mp.setitem(sys.modules, "httpx", self.http.module())
        self.n += 1
        spec = importlib.util.spec_from_file_location(f"hotpicks_store_under_test_{self.n}", _STORE_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.time = self.clock          # deterministic clock; repair loops never really sleep
        return mod


@pytest.fixture
def env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return Env(monkeypatch)


@pytest.fixture
def m(env):
    return env.load()


# ── row builders ──────────────────────────────────────────────────────────────

_RECENT_KEYS = ("SYMBOL", "SECTION", "DECISION", "SCORE", "NEWS_SCORE", "HEADLINE_COUNT",
                "SIGNAL_STRENGTH", "FROM_SCAN", "NEXT_EARNINGS_DATE", "SUMMARY", "ITEM_JSON",
                "GENERATED_AT", "UPDATED_AT")


def rec(**kw):
    """One stored row as a tuple in _RECENT_KEYS order (Oracle-style UPPER-CASE labels)."""
    d = {
        "SYMBOL": "AAA", "SECTION": "news_driven", "DECISION": "BUY", "SCORE": 70,
        "NEWS_SCORE": None, "HEADLINE_COUNT": None, "SIGNAL_STRENGTH": None, "FROM_SCAN": 1,
        "NEXT_EARNINGS_DATE": None, "SUMMARY": None, "ITEM_JSON": None,
        "GENERATED_AT": "2026-09-30T10:00:00", "UPDATED_AT": NOW - timedelta(hours=2),
    }
    d.update({k.upper(): v for k, v in kw.items()})
    return tuple(d[k] for k in _RECENT_KEYS)


def rowmap(**kw):
    """A lower-cased row_map, as _row_to_item receives it."""
    d = {"symbol": "AAA", "section": "news_driven", "decision": "BUY", "score": 70,
         "news_score": None, "headline_count": None, "signal_strength": None, "from_scan": 1,
         "next_earnings_date": None, "summary": None, "item_json": None,
         "generated_at": "2026-09-30T10:00:00", "updated_at": NOW}
    d.update(kw)
    return d


# ── import-time constants ─────────────────────────────────────────────────────

class TestConstants:
    def test_defaults(self, m):
        assert m.HOTPICKS_DB_FRESH_HOURS == 24.0
        assert m.HOTPICKS_RETENTION_HOURS == 72.0
        assert m.HOTPICKS_TABLE_HOURS == 24.0
        assert m.HOTPICKS_AUDIT_TTL_SEC == 20.0
        assert m.SECTIONS == ("news_driven", "results_driven", "bulk_insider_driven")

    def test_env_overrides(self, env):
        mod = env.load(HOTPICKS_DB_FRESH_HOURS="6.5", HOTPICKS_RETENTION_HOURS="48",
                       HOTPICKS_TABLE_HOURS="12", HOTPICKS_AUDIT_TTL_SEC="3")
        assert (mod.HOTPICKS_DB_FRESH_HOURS, mod.HOTPICKS_RETENTION_HOURS,
                mod.HOTPICKS_TABLE_HOURS, mod.HOTPICKS_AUDIT_TTL_SEC) == (6.5, 48.0, 12.0, 3.0)


# ── stop flag ─────────────────────────────────────────────────────────────────

class TestStopFlag:
    def test_request_clear_cycle(self, m):
        assert m.hotpicks_stop_requested() is False
        m.request_hotpicks_stop()
        assert m.hotpicks_stop_requested() is True
        m.request_hotpicks_stop()                      # idempotent
        assert m.hotpicks_stop_requested() is True
        m.clear_hotpicks_stop()
        assert m.hotpicks_stop_requested() is False

    def test_flag_is_per_module_copy(self, env):
        a, b = env.load(), env.load()
        a.request_hotpicks_stop()
        assert a.hotpicks_stop_requested() is True
        assert b.hotpicks_stop_requested() is False


# ── _schema ───────────────────────────────────────────────────────────────────

class TestSchemaLoader:
    def test_returns_module(self, env):
        mod = env.load()
        assert mod._schema() is env.hp

    def test_missing_module_raises_import_error(self, env):
        mod = env.load(no_schema=True)
        with pytest.raises(ImportError):
            mod._schema()


# ── _lob_to_str / _num / _int / _utc_hours_since ──────────────────────────────

class _Lob:
    def __init__(self, data=None, raises=False):
        self._data, self._raises = data, raises

    def read(self):
        if self._raises:
            raise RuntimeError("lob read boom")
        return self._data

    def __str__(self):
        return "LOB-STR"


class _NoRead:
    def __str__(self):
        return "NO-READ"


class _ReadNotCallable:
    read = "nope"

    def __str__(self):
        return "READ-NOT-CALLABLE"


class TestLobToStr:
    def test_none_and_str_pass_through(self, m):
        assert m._lob_to_str(None) is None
        assert m._lob_to_str("abc") == "abc"
        assert m._lob_to_str("") == ""

    def test_reads_lob(self, m):
        assert m._lob_to_str(_Lob("payload")) == "payload"

    def test_read_failure_gives_none(self, m):
        assert m._lob_to_str(_Lob(raises=True)) is None

    def test_no_read_falls_back_to_str(self, m):
        assert m._lob_to_str(_NoRead()) == "NO-READ"
        assert m._lob_to_str(_ReadNotCallable()) == "READ-NOT-CALLABLE"
        assert m._lob_to_str(123) == "123"


class TestNum:
    @pytest.mark.parametrize("value, expected", [
        (None, None), (5, 5.0), ("3.5", 3.5), (Decimal("75.00"), 75.0), (0, 0.0),
        (True, 1.0), ("abc", None), ("", None), (object(), None), ([], None),
    ])
    def test_cases(self, m, value, expected):
        assert m._num(value) == expected

    def test_returns_float_type(self, m):
        assert type(m._num(Decimal("75"))) is float


class TestInt:
    @pytest.mark.parametrize("value, expected", [
        (None, 0), (7, 7), ("12", 12), (3.9, 3), (Decimal("4"), 4), ("x", 0), ("3.5", 0),
        ([], 0), (object(), 0),
    ])
    def test_cases(self, m, value, expected):
        assert m._int(value) == expected


class TestUtcHoursSince:
    def test_none(self, m):
        assert m._utc_hours_since(None) is None

    def test_aware(self, m):
        got = m._utc_hours_since(datetime.now(timezone.utc) - timedelta(hours=3))
        assert got == pytest.approx(3.0, abs=0.02)

    def test_naive_is_treated_as_utc(self, m):
        naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=5)
        assert m._utc_hours_since(naive) == pytest.approx(5.0, abs=0.02)

    def test_non_utc_aware_uses_its_own_offset(self, m):
        ist = timezone(timedelta(hours=5, minutes=30))
        got = m._utc_hours_since(datetime.now(ist) - timedelta(hours=1))
        assert got == pytest.approx(1.0, abs=0.02)

    def test_future_timestamp_is_negative(self, m):
        got = m._utc_hours_since(datetime.now(timezone.utc) + timedelta(hours=2))
        assert got < 0

    @pytest.mark.parametrize("bad", ["2026-09-30", 12345, object()])
    def test_unusable_value_gives_none(self, m, bad):
        assert m._utc_hours_since(bad) is None


# ── _row_to_item ──────────────────────────────────────────────────────────────

class TestRowToItem:
    def test_json_is_authoritative_and_columns_only_fill_gaps(self, m, env):
        blob = {"symbol": "ZZZ", "section": "results_driven", "decision": "SELL", "score": 12,
                "news_score": 3, "headline_count": 9, "signal_strength": "weak",
                "summary": "from json", "next_earnings_date": "2026-11-01", "extra": "kept"}
        row = rowmap(item_json=json.dumps(blob), score=99, news_score=98, headline_count=97,
                     signal_strength="strong", summary="column", next_earnings_date="2030-01-01")
        item = m._row_to_item(row, env.hp)
        for k, v in blob.items():
            assert item[k] == v

    def test_zero_score_in_json_is_kept(self, m, env):
        item = m._row_to_item(rowmap(item_json='{"score": 0}', score=55), env.hp)
        assert item["score"] == 0

    def test_columns_fill_when_json_missing(self, m, env):
        row = rowmap(item_json=None, symbol="RELI", section="bulk_insider_driven", decision="HOLD",
                     score=Decimal("64.50"), news_score=Decimal("7"), headline_count="4",
                     signal_strength="mid", summary="col summary", next_earnings_date="2026-10-20",
                     from_scan=0)
        item = m._row_to_item(row, env.hp)
        assert item["symbol"] == "RELI"
        assert item["section"] == "bulk_insider_driven"
        assert item["decision"] == "HOLD"
        assert item["score"] == 64.5 and type(item["score"]) is float
        assert item["news_score"] == 7.0
        assert item["headline_count"] == 4
        assert item["signal_strength"] == "mid"
        assert item["summary"] == "col summary"
        assert item["next_earnings_date"] == "2026-10-20"
        assert item["from_scan"] is False

    @pytest.mark.parametrize("raw", ["not json", "{", "[1, 2]", '"str"', "123", "null"])
    def test_bad_or_non_dict_json_falls_back_to_columns(self, m, env, raw):
        item = m._row_to_item(rowmap(item_json=raw, symbol="ABC", score=41), env.hp)
        assert item["symbol"] == "ABC"
        assert item["score"] == 41.0

    def test_empty_json_string_falls_back(self, m, env):
        item = m._row_to_item(rowmap(item_json="", symbol="ABC"), env.hp)
        assert item["symbol"] == "ABC"

    def test_clob_json_is_read(self, m, env):
        item = m._row_to_item(rowmap(item_json=_Lob('{"symbol": "CLOB", "x": 1}')), env.hp)
        assert item["symbol"] == "CLOB" and item["x"] == 1

    def test_unreadable_clob_falls_back(self, m, env):
        item = m._row_to_item(rowmap(item_json=_Lob(raises=True), symbol="ABC"), env.hp)
        assert item["symbol"] == "ABC"

    def test_null_score_everywhere_stays_none(self, m, env):
        item = m._row_to_item(rowmap(item_json='{"score": null}', score=None), env.hp)
        assert item["score"] is None

    def test_json_null_score_uses_column(self, m, env):
        item = m._row_to_item(rowmap(item_json='{"score": null}', score=33), env.hp)
        assert item["score"] == 33.0

    def test_news_score_absent_when_neither_source_has_it(self, m, env):
        item = m._row_to_item(rowmap(news_score=None), env.hp)
        assert "news_score" not in item

    def test_news_score_from_column_when_json_null(self, m, env):
        item = m._row_to_item(rowmap(item_json='{"news_score": null}', news_score=5), env.hp)
        assert item["news_score"] == 5.0

    def test_headline_count_defaults_to_zero(self, m, env):
        assert m._row_to_item(rowmap(headline_count=None), env.hp)["headline_count"] == 0
        assert m._row_to_item(rowmap(headline_count="oops"), env.hp)["headline_count"] == 0

    def test_zero_headline_count_in_json_kept(self, m, env):
        item = m._row_to_item(rowmap(item_json='{"headline_count": 0}', headline_count=8), env.hp)
        assert item["headline_count"] == 0

    def test_summary_defaults_to_empty_string(self, m, env):
        assert m._row_to_item(rowmap(summary=None), env.hp)["summary"] == ""

    def test_summary_clob_column_is_read(self, m, env):
        assert m._row_to_item(rowmap(summary=_Lob("long text")), env.hp)["summary"] == "long text"

    def test_next_earnings_date_rules(self, m, env):
        # column fills a missing/empty item value ...
        assert m._row_to_item(rowmap(next_earnings_date="2026-12-01"), env.hp)["next_earnings_date"] == "2026-12-01"
        item = m._row_to_item(rowmap(item_json='{"next_earnings_date": ""}', next_earnings_date="2026-12-01"), env.hp)
        assert item["next_earnings_date"] == "2026-12-01"
        # ... never overrides one the JSON already has ...
        item = m._row_to_item(rowmap(item_json='{"next_earnings_date": "2026-11-11"}',
                                     next_earnings_date="2026-12-01"), env.hp)
        assert item["next_earnings_date"] == "2026-11-11"
        # ... and nothing is invented when the column is empty
        assert "next_earnings_date" not in m._row_to_item(rowmap(next_earnings_date=None), env.hp)
        assert "next_earnings_date" not in m._row_to_item(rowmap(next_earnings_date=""), env.hp)

    def test_from_scan_always_comes_from_the_column_via_schema(self, m, env):
        item = m._row_to_item(rowmap(item_json='{"from_scan": true}', from_scan=0), env.hp)
        assert item["from_scan"] is False
        item = m._row_to_item(rowmap(item_json='{"from_scan": false}', from_scan=1), env.hp)
        assert item["from_scan"] is True

    def test_stored_at_isoformat_or_raw(self, m, env):
        stamp = datetime(2026, 9, 30, 4, 5, 6, tzinfo=timezone.utc)
        assert m._row_to_item(rowmap(updated_at=stamp), env.hp)["stored_at"] == stamp.isoformat()
        assert m._row_to_item(rowmap(updated_at="2026-09-30 04:05"), env.hp)["stored_at"] == "2026-09-30 04:05"
        assert m._row_to_item(rowmap(updated_at=None), env.hp)["stored_at"] is None

    def test_stored_generated_at_passthrough(self, m, env):
        assert m._row_to_item(rowmap(generated_at="G1"), env.hp)["stored_generated_at"] == "G1"
        assert m._row_to_item(rowmap(generated_at=None), env.hp)["stored_generated_at"] is None

    def test_signal_strength_and_decision_setdefault(self, m, env):
        item = m._row_to_item(rowmap(item_json='{"decision": "SELL"}', decision="BUY", signal_strength="s"), env.hp)
        assert item["decision"] == "SELL" and item["signal_strength"] == "s"


# ── _payload_rows ─────────────────────────────────────────────────────────────

class TestPayloadRows:
    def test_flattens_every_section_in_declared_order(self, m):
        payload = {
            "generated_at": "2026-09-30T09:00:00",
            "bulk_insider_driven": [{"symbol": "cc"}],
            "results_driven": [{"symbol": "bb"}],
            "news_driven": [{"symbol": "aa"}],
        }
        rows = m._payload_rows(payload)
        assert [(r["symbol"], r["section"]) for r in rows] == [
            ("AA", "news_driven"), ("BB", "results_driven"), ("CC", "bulk_insider_driven")]

    def test_row_values(self, m):
        item = {"symbol": " reliance ", "decision": "BUY", "score": "71.5", "news_score": Decimal("4"),
                "headline_count": "6", "signal_strength": "strong", "from_scan": 1,
                "next_earnings_date": "2026-11-05T00:00:00+00:00-and-more-than-20-chars",
                "summary": "Good quarter", "extra": {"a": 1}}
        (r,) = m._payload_rows({"generated_at": "GEN", "news_driven": [item]})
        assert r["symbol"] == "RELIANCE"
        assert r["decision"] == "BUY"
        assert r["score"] == 71.5
        assert r["news_score"] == 4.0
        assert r["headline_count"] == 6
        assert r["signal_strength"] == "strong"
        assert r["from_scan"] is True
        assert r["next_earnings_date"] == item["next_earnings_date"][:20]
        assert len(r["next_earnings_date"]) == 20
        assert r["summary"] == "Good quarter"
        assert r["generated_at"] == "GEN"
        assert json.loads(r["item_json"]) == dict(item, news_score="4")   # Decimal -> str via default=str

    def test_defaults_for_a_bare_item(self, m):
        (r,) = m._payload_rows({"news_driven": [{"symbol": "x"}]})
        assert r["decision"] is None and r["score"] is None and r["news_score"] is None
        assert r["headline_count"] == 0
        assert r["signal_strength"] is None
        assert r["from_scan"] is False
        assert r["next_earnings_date"] is None
        assert r["summary"] == ""
        assert r["generated_at"] == ""

    def test_summary_none_becomes_empty_string(self, m):
        (r,) = m._payload_rows({"news_driven": [{"symbol": "x", "summary": None}]})
        assert r["summary"] == ""

    def test_generated_at_is_clipped_to_40(self, m):
        (r,) = m._payload_rows({"generated_at": "g" * 60, "news_driven": [{"symbol": "x"}]})
        assert r["generated_at"] == "g" * 40

    def test_generated_at_none_is_empty(self, m):
        (r,) = m._payload_rows({"generated_at": None, "news_driven": [{"symbol": "x"}]})
        assert r["generated_at"] == ""

    @pytest.mark.parametrize("bad_item", [None, "AAA", 5, ["AAA"], {}, {"symbol": None},
                                          {"symbol": ""}, {"symbol": "   "}, {"decision": "BUY"}])
    def test_skips_non_dicts_and_blank_symbols(self, m, bad_item):
        assert m._payload_rows({"news_driven": [bad_item]}) == []

    def test_missing_none_or_empty_sections(self, m):
        assert m._payload_rows({}) == []
        assert m._payload_rows({"news_driven": None, "results_driven": [], "bulk_insider_driven": None}) == []

    def test_unknown_sections_are_ignored(self, m):
        assert m._payload_rows({"other_section": [{"symbol": "AAA"}]}) == []

    def test_same_symbol_in_two_sections_gives_two_rows(self, m):
        rows = m._payload_rows({"news_driven": [{"symbol": "A"}], "results_driven": [{"symbol": "A"}]})
        assert [r["section"] for r in rows] == ["news_driven", "results_driven"]

    def test_unserialisable_item_json_becomes_none(self, m):
        # tuple keys make json.dumps raise even with default=str
        (r,) = m._payload_rows({"news_driven": [{"symbol": "A", (1, 2): "x"}]})
        assert r["item_json"] is None and r["symbol"] == "A"

    def test_non_json_values_are_stringified(self, m):
        stamp = datetime(2026, 9, 30, tzinfo=timezone.utc)
        (r,) = m._payload_rows({"news_driven": [{"symbol": "A", "when": stamp}]})
        assert json.loads(r["item_json"])["when"] == str(stamp)

    def test_every_declared_bind_key_exists(self, m):
        (r,) = m._payload_rows({"news_driven": [{"symbol": "A"}]})
        assert set(r) >= set(FakeHP.ROW_KEYS)
        assert r["extra_key"] is None

    def test_bad_numbers_become_none_not_errors(self, m):
        (r,) = m._payload_rows({"news_driven": [{"symbol": "A", "score": "n/a", "news_score": "x",
                                                  "headline_count": "many"}]})
        assert r["score"] is None and r["news_score"] is None and r["headline_count"] == 0


# ── hotpicks_db_upsert ────────────────────────────────────────────────────────

def _payload(*symbols, section="news_driven"):
    return {"generated_at": "GEN", section: [{"symbol": s, "decision": "BUY", "score": 50} for s in symbols]}


class TestUpsert:
    @pytest.mark.parametrize("bad", [None, [], "x", 5, ()])
    def test_non_dict_payload_returns_zero(self, m, env, bad):
        assert m.hotpicks_db_upsert(bad) == 0
        assert env.hp.made == []

    def test_schema_unavailable(self, env):
        mod = env.load(no_schema=True)
        assert mod.hotpicks_db_upsert(_payload("A")) == 0

    def test_no_database_configured(self, m, env):
        env.hp.url = None
        assert m.hotpicks_db_upsert(_payload("A")) == 0
        assert env.hp.made == []

    def test_no_rows_never_builds_an_engine(self, m, env):
        assert m.hotpicks_db_upsert({"generated_at": "x"}) == 0
        assert m.hotpicks_db_upsert({"news_driven": [{"symbol": ""}]}) == 0
        assert env.hp.made == []

    def test_engine_none_returns_zero(self, m, env):
        env.hp.writer_none = True
        assert m.hotpicks_db_upsert(_payload("A")) == 0
        assert env.hp.writer.begins == 0

    def test_postgres_executemany_then_prune(self, m, env):
        n = m.hotpicks_db_upsert(_payload("A", "B", "C"))
        eng = env.hp.writer
        assert n == 3
        assert env.hp.made == ["stockky-hotpicks-writer"]
        assert env.hp.adapted == ["postgresql"]
        assert eng.begins == 2                                   # write txn + separate prune txn
        upsert, prune = eng.calls
        assert upsert[0] == "UPSERT[postgresql]"
        assert isinstance(upsert[1], list) and [r["symbol"] for r in upsert[1]] == ["A", "B", "C"]
        assert all(r["adapted_for"] == "postgresql" for r in upsert[1])
        assert prune == ("DELETE_OLD[postgresql]", {"hours": 72})
        assert eng.disposed == 1

    def test_oracle_row_by_row(self, m, env):
        env.hp.dial = "oracle"
        n = m.hotpicks_db_upsert(_payload("A", "B"))
        eng = env.hp.writer
        assert n == 2
        assert env.hp.adapted == ["oracle"]
        upserts = eng.find("UPSERT")
        assert len(upserts) == 2
        assert all(isinstance(c[1], dict) for c in upserts)       # one bind dict per statement
        assert [c[1]["symbol"] for c in upserts] == ["A", "B"]
        assert eng.begins == 2                                    # one write txn (all rows) + prune
        assert eng.sqls()[-1] == "DELETE_OLD[oracle]"

    def test_retention_hours_int_truncated_from_env(self, env):
        mod = env.load(HOTPICKS_RETENTION_HOURS="48.9")
        mod.hotpicks_db_upsert(_payload("A"))
        assert env.hp.writer.calls[-1][1] == {"hours": 48}

    def test_prune_failure_is_swallowed_and_count_kept(self, m, env, caplog):
        env.hp.writer.raises_on = [("DELETE_OLD", RuntimeError("prune boom"))]
        with caplog.at_level(logging.DEBUG, logger="hotpicks-store"):
            assert m.hotpicks_db_upsert(_payload("A", "B")) == 2
        assert any("hotpicks prune skipped" in r.getMessage() for r in caplog.records)
        assert env.hp.writer.disposed == 1

    def test_write_failure_returns_zero_warns_and_disposes(self, m, env, caplog):
        env.hp.writer.raises_on = [("UPSERT", RuntimeError("write boom"))]
        with caplog.at_level(logging.WARNING, logger="hotpicks-store"):
            assert m.hotpicks_db_upsert(_payload("A")) == 0
        assert any("hotpicks db upsert failed" in r.getMessage() and "write boom" in r.getMessage()
                   for r in caplog.records)
        assert env.hp.writer.disposed == 1
        assert env.hp.writer.find("DELETE") == []                 # no prune after a failed write

    def test_oracle_failure_mid_way_returns_zero(self, m, env):
        env.hp.dial = "oracle"
        env.hp.writer.raises_on = [("UPSERT", RuntimeError("ORA-12899"))]
        assert m.hotpicks_db_upsert(_payload("A", "B")) == 0

    def test_dispose_failure_is_swallowed(self, m, env):
        env.hp.writer.dispose_raises = True
        assert m.hotpicks_db_upsert(_payload("A")) == 1
        assert env.hp.writer.disposed == 1

    def test_missing_sqlalchemy_returns_zero(self, env):
        mod = env.load(no_sa=True)
        assert mod.hotpicks_db_upsert(_payload("A")) == 0
        assert env.hp.made == []


# ── hotpicks_db_payload ───────────────────────────────────────────────────────

def _reader(env, rows, keys=_RECENT_KEYS):
    env.hp.shared.recent_keys = list(keys)
    env.hp.shared.recent_rows = rows
    return env.hp.shared


class TestDbPayload:
    def test_schema_unavailable(self, env):
        assert env.load(no_schema=True).hotpicks_db_payload() is None

    def test_sqlalchemy_unavailable(self, env):
        assert env.load(no_sa=True).hotpicks_db_payload() is None

    def test_no_database(self, m, env):
        env.hp.url = None
        assert m.hotpicks_db_payload() is None
        assert env.hp.shared_apps == []

    def test_engine_none(self, m, env):
        env.hp.shared_none = True
        assert m.hotpicks_db_payload() is None

    def test_table_missing(self, m, env):
        env.hp.shared.exists = False
        assert m.hotpicks_db_payload() is None
        assert env.hp.shared.find("SELECT_RECENT") == []

    def test_empty_table(self, m, env):
        _reader(env, [])
        assert m.hotpicks_db_payload() is None

    def test_only_unknown_sections_counts_as_empty(self, m, env):
        _reader(env, [rec(section="weird"), rec(section="")])
        assert m.hotpicks_db_payload() is None

    def test_uses_shared_reader_engine_and_never_disposes_it(self, m, env):
        _reader(env, [rec()])
        m.hotpicks_db_payload()
        assert env.hp.shared_apps == ["stockky-hotpicks-reader"]
        assert env.hp.shared.disposed == 0
        assert env.hp.shared.connects == 1 and env.hp.shared.begins == 0

    def test_queries_use_dialect_table_and_hours(self, m, env):
        env.hp.dial = "oracle"
        _reader(env, [rec()])
        m.hotpicks_db_payload(hours=6.7)
        exists, recent = env.hp.shared.calls
        assert exists == ("EXISTS[oracle]", {"tbl": "hotpicks_static_feed"})
        assert recent == ("SELECT_RECENT[oracle]", {"hours": 6})

    def test_default_window_is_table_hours(self, env):
        mod = env.load(HOTPICKS_TABLE_HOURS="12")
        _reader(env, [rec()])
        assert mod.hotpicks_db_payload()["hours"] == 12

    def test_explicit_zero_hours_is_honoured(self, m, env):
        _reader(env, [rec()])
        out = m.hotpicks_db_payload(hours=0)
        assert out["hours"] == 0
        assert env.hp.shared.calls[-1][1] == {"hours": 0}

    def test_builds_sections_and_metadata(self, m, env):
        _reader(env, [
            rec(symbol="N1", section="news_driven", generated_at="2026-09-30T08:00:00",
                updated_at=NOW - timedelta(hours=5)),
            rec(symbol="R1", section="RESULTS_DRIVEN", generated_at="2026-09-30T11:00:00",
                updated_at=NOW - timedelta(hours=1)),
            rec(symbol="B1", section="bulk_insider_driven", generated_at="2026-09-30T09:00:00",
                updated_at=NOW - timedelta(hours=3)),
            rec(symbol="N2", section="news_driven", generated_at=None,
                updated_at=NOW - timedelta(hours=4)),
            rec(symbol="X1", section="unknown_section"),
        ])
        out = m.hotpicks_db_payload()
        assert [i["symbol"] for i in out["news_driven"]] == ["N1", "N2"]
        assert [i["symbol"] for i in out["results_driven"]] == ["R1"]
        assert [i["symbol"] for i in out["bulk_insider_driven"]] == ["B1"]
        assert out["count"] == 4                                   # unknown section excluded
        assert out["generated_at"] == "2026-09-30T11:00:00"        # newest string wins
        assert out["hours"] == 24
        assert out["age_hours"] == pytest.approx(1.0, abs=0.05)    # from the newest updated_at
        assert out["fresh"] is True
        assert out["source"] == "hotpicks_static_feed"
        assert out["backend"] == "postgresql"
        assert out["cached"] is True
        assert set(out) == {"news_driven", "results_driven", "bulk_insider_driven", "generated_at",
                            "count", "hours", "age_hours", "fresh", "source", "backend", "cached"}

    def test_newest_generated_at_is_order_independent(self, m, env):
        _reader(env, [rec(symbol="A", generated_at="2026-09-30T11:00:00"),
                      rec(symbol="B", generated_at="2026-09-30T08:00:00")])
        assert m.hotpicks_db_payload()["generated_at"] == "2026-09-30T11:00:00"

    def test_generated_at_none_when_no_row_has_one(self, m, env):
        _reader(env, [rec(generated_at=None), rec(generated_at="")])
        assert m.hotpicks_db_payload()["generated_at"] is None

    def test_last_at_is_the_max_regardless_of_order(self, m, env):
        _reader(env, [rec(symbol="A", updated_at=NOW - timedelta(hours=1)),
                      rec(symbol="B", updated_at=NOW - timedelta(hours=9))])
        assert m.hotpicks_db_payload()["age_hours"] == pytest.approx(1.0, abs=0.05)

    def test_stale_rows_are_not_fresh(self, m, env):
        _reader(env, [rec(updated_at=NOW - timedelta(hours=30))])
        out = m.hotpicks_db_payload()
        assert out["fresh"] is False
        assert out["age_hours"] == pytest.approx(30.0, abs=0.05)

    def test_fresh_threshold_is_env_driven(self, env):
        mod = env.load(HOTPICKS_DB_FRESH_HOURS="1")
        _reader(env, [rec(updated_at=NOW - timedelta(hours=2))])
        assert mod.hotpicks_db_payload()["fresh"] is False

    def test_age_is_rounded_to_two_decimals(self, m, env, monkeypatch):
        _reader(env, [rec()])
        monkeypatch.setattr(m, "_utc_hours_since", lambda _v: 1.23456)
        assert m.hotpicks_db_payload()["age_hours"] == 1.23

    def test_age_exactly_at_threshold_is_fresh(self, m, env, monkeypatch):
        _reader(env, [rec()])
        monkeypatch.setattr(m, "_utc_hours_since", lambda _v: 24.0)
        assert m.hotpicks_db_payload()["fresh"] is True
        monkeypatch.setattr(m, "_utc_hours_since", lambda _v: 24.01)
        assert m.hotpicks_db_payload()["fresh"] is False

    def test_naive_oracle_timestamps_work(self, m, env):
        naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
        _reader(env, [rec(updated_at=naive)])
        out = m.hotpicks_db_payload()
        assert out["age_hours"] == pytest.approx(2.0, abs=0.05) and out["fresh"] is True

    def test_all_updated_at_none_gives_unknown_age(self, m, env):
        _reader(env, [rec(updated_at=None)])
        out = m.hotpicks_db_payload()
        assert out["age_hours"] is None and out["fresh"] is False and out["count"] == 1

    def test_keys_are_lowercased_per_row(self, m, env):
        _reader(env, [rec(symbol="OK", item_json='{"symbol": "OK", "price": 5}')])
        (item,) = m.hotpicks_db_payload()["news_driven"]
        assert item["price"] == 5 and item["from_scan"] is True

    def test_query_failure_returns_none_and_warns(self, m, env, caplog):
        env.hp.shared.raises_on = [("SELECT_RECENT", RuntimeError("db down"))]
        with caplog.at_level(logging.WARNING, logger="hotpicks-store"):
            assert m.hotpicks_db_payload() is None
        assert any("hotpicks db read failed" in r.getMessage() for r in caplog.records)


# ── hotpicks_db_freshness_hours ───────────────────────────────────────────────

class TestFreshnessHours:
    def test_schema_unavailable(self, env):
        assert env.load(no_schema=True).hotpicks_db_freshness_hours() is None

    def test_sqlalchemy_unavailable(self, env):
        assert env.load(no_sa=True).hotpicks_db_freshness_hours() is None

    def test_no_database(self, m, env):
        env.hp.url = None
        assert m.hotpicks_db_freshness_hours() is None

    def test_engine_none(self, m, env):
        env.hp.shared_none = True
        assert m.hotpicks_db_freshness_hours() is None

    def test_table_missing(self, m, env):
        env.hp.shared.exists = False
        assert m.hotpicks_db_freshness_hours() is None

    def test_value(self, m, env):
        env.hp.shared.max_row = (NOW - timedelta(hours=7),)
        assert m.hotpicks_db_freshness_hours() == pytest.approx(7.0, abs=0.05)
        assert env.hp.shared_apps == ["stockky-hotpicks-freshness"]
        assert env.hp.shared.calls[-1][0] == "SELECT MAX(updated_at) FROM hotpicks_static_feed"

    def test_naive_timestamp(self, m, env):
        env.hp.shared.max_row = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3),)
        assert m.hotpicks_db_freshness_hours() == pytest.approx(3.0, abs=0.05)

    def test_no_row_or_null_max_is_none(self, m, env):
        env.hp.shared.max_row = None
        assert m.hotpicks_db_freshness_hours() is None
        env.hp.shared.max_row = (None,)
        assert m.hotpicks_db_freshness_hours() is None

    def test_query_failure(self, m, env):
        env.hp.shared.raises_on = [("MAX(updated_at)", RuntimeError("boom"))]
        assert m.hotpicks_db_freshness_hours() is None


# ── hotpicks_audit (memo) + _hotpicks_audit_uncached ─────────────────────────

class TestAuditMemo:
    def test_first_call_computes_second_serves_cache(self, m, env):
        env.hp.shared.max_row = (NOW,)
        first = m.hotpicks_audit()
        n_calls = len(env.hp.shared.calls)
        second = m.hotpicks_audit()
        assert first["cached"] is False
        assert second["cached"] is True
        assert len(env.hp.shared.calls) == n_calls                 # no new queries
        assert {k: v for k, v in first.items() if k != "cached"} == \
               {k: v for k, v in second.items() if k != "cached"}

    def test_ttl_boundary_is_exclusive(self, m, env):
        env.hp.shared.max_row = (NOW,)
        m.hotpicks_audit()
        n = len(env.hp.shared.calls)
        env.clock.now += 19.99
        assert m.hotpicks_audit()["cached"] is True
        assert len(env.hp.shared.calls) == n
        env.clock.now = 1000.0 + 20.0                              # exactly the TTL -> recompute
        assert m.hotpicks_audit()["cached"] is False
        assert len(env.hp.shared.calls) > n

    def test_ttl_is_env_driven(self, env):
        mod = env.load(HOTPICKS_AUDIT_TTL_SEC="5")
        env.hp.shared.max_row = (NOW,)
        mod.hotpicks_audit()
        env.clock.now += 6
        assert mod.hotpicks_audit()["cached"] is False

    def test_recompute_restamps_the_cache(self, m, env):
        env.hp.shared.max_row = (NOW,)
        m.hotpicks_audit()
        env.clock.now += 100
        m.hotpicks_audit()                                         # recompute at t+100
        env.clock.now += 10                                        # 10s after the restamp
        assert m.hotpicks_audit()["cached"] is True

    def test_callers_cannot_corrupt_the_cache(self, m, env):
        env.hp.shared.max_row = (NOW,)
        first = m.hotpicks_audit()
        first["cached"] = "tampered"
        first["ok"] = "tampered"
        second = m.hotpicks_audit()
        assert second["cached"] is True and second["ok"] is True

    def test_failed_audits_are_cached_too(self, m, env):
        env.hp.url = None
        a = m.hotpicks_audit()
        env.hp.url = "postgresql://fake"                           # fixed within the TTL...
        b = m.hotpicks_audit()
        assert b["cached"] is True and b["configured"] is False    # ...but the memo still answers


def _audit(env, mod, rows, count=None, max_row="auto"):
    e = env.hp.shared
    e.recent_keys = list(_RECENT_KEYS)
    e.recent_rows = rows
    e.count = len(rows) if count is None else count
    e.max_row = (NOW - timedelta(hours=2),) if max_row == "auto" else max_row
    return mod._hotpicks_audit_uncached()


class TestAuditUncached:
    def test_baseline_shape(self, env):
        env.hp.url = None
        out = env.load()._hotpicks_audit_uncached()
        assert set(out) == {
            "ok", "table", "backend", "configured", "table_exists", "rows_total", "rows_24h",
            "by_section", "age_hours", "fresh", "fresh_threshold_hours", "retention_hours",
            "missing_decision", "missing_score", "missing_price", "issues", "health_score",
            "total_tracked", "fully_populated", "missing_data", "incomplete_stocks"}

    def test_schema_unavailable(self, env):
        out = env.load(no_schema=True)._hotpicks_audit_uncached()
        assert out["ok"] is False and out["table"] is None and out["backend"] is None
        assert len(out["issues"]) == 1 and out["issues"][0].startswith("hotpicks_schema unavailable: ")
        assert out["health_score"] == 0.0 and out["incomplete_stocks"] == []

    def test_schema_unavailable_message_is_clipped_to_120(self, env, monkeypatch):
        mod = env.load()
        monkeypatch.setattr(mod, "_schema", lambda: (_ for _ in ()).throw(ImportError("x" * 500)))
        out = mod._hotpicks_audit_uncached()
        assert out["issues"][0] == "hotpicks_schema unavailable: " + "x" * 120

    def test_sqlalchemy_unavailable(self, env):
        out = env.load(no_sa=True)._hotpicks_audit_uncached()
        assert out["ok"] is False and out["issues"][0].startswith("hotpicks_schema unavailable")

    def test_not_configured(self, env):
        env.hp.url = None
        env.hp.dial = "oracle"
        out = env.load()._hotpicks_audit_uncached()
        assert out["ok"] is False and out["configured"] is False
        assert out["table"] == "hotpicks_static_feed" and out["backend"] == "oracle"
        assert len(out["issues"]) == 1 and "memory only" in out["issues"][0]
        assert "CACHE_DATABASE_URL" in out["issues"][0]

    def test_engine_none(self, m, env):
        env.hp.shared_none = True
        out = m._hotpicks_audit_uncached()
        assert out["configured"] is True and out["ok"] is False
        assert out["issues"] == ["Could not build a database engine"]

    def test_table_missing_is_ok(self, m, env):
        env.hp.shared.exists = False
        out = m._hotpicks_audit_uncached()
        assert out["ok"] is True and out["table_exists"] is False and out["configured"] is True
        assert len(out["issues"]) == 1 and "does not exist yet" in out["issues"][0]
        assert "hotpicks_static_feed" in out["issues"][0]
        assert out["rows_total"] == 0

    def test_uses_shared_audit_engine(self, m, env):
        _audit(env, m, [rec()])
        assert env.hp.shared_apps == ["stockky-hotpicks-audit"]
        assert env.hp.shared.disposed == 0
        recent = env.hp.shared.find("SELECT_RECENT")
        assert recent[0][1] == {"hours": 24}

    def test_counts_and_incomplete_listing(self, m, env):
        def p(price=None, close=None):
            d = {}
            if price is not None:
                d["price"] = price
            if close is not None:
                d["close"] = close
            return json.dumps(d)

        rows = [
            rec(symbol="A", section="news_driven", item_json=p(price=100)),                 # complete
            rec(symbol="B", section="results_driven", item_json=p(close=50)),               # complete via close
            rec(symbol="C", section="bulk_insider_driven", decision="", item_json=p(price=10)),  # no decision
            rec(symbol="D", section="news_driven", score=None, item_json=p(price=10)),      # no score
            rec(symbol="E", section="news_driven", item_json="{}"),                         # no price
            rec(symbol="F", section="news_driven", item_json="not json"),                   # no price
            rec(symbol="G", section="weird_section", item_json=p(price=1)),                 # complete, unknown sec
            rec(symbol="H", section="news_driven", item_json=None),                         # no price
            rec(symbol="I", section="news_driven", score=0, item_json=p(price=5)),          # score 0 is a score
            rec(symbol="J", section="news_driven", item_json=p(price=0, close=7)),          # price 0 -> close
            rec(symbol="K", section="news_driven", item_json="[1]"),                        # not a dict
            rec(symbol="L", section="news_driven", decision="", score=None, item_json="{}"),  # all three
        ]
        out = _audit(env, m, rows, count=99)
        assert out["ok"] is True and out["table_exists"] is True
        assert out["rows_total"] == 99                              # COUNT(*), not len(rows)
        assert out["rows_24h"] == 12
        assert out["by_section"] == {"news_driven": 9, "results_driven": 1, "bulk_insider_driven": 1}
        assert out["missing_decision"] == 2
        assert out["missing_score"] == 2
        assert out["missing_price"] == 5
        assert out["fully_populated"] == 5
        assert out["total_tracked"] == 12
        assert out["missing_data"] == 5                             # price-only, by design
        assert out["health_score"] == round(5 / 12 * 100, 1) == 41.7
        assert out["incomplete_stocks"] == [
            {"symbol": "C", "missing_fields": ["decision"]},
            {"symbol": "D", "missing_fields": ["score"]},
            {"symbol": "E", "missing_fields": ["price"]},
            {"symbol": "F", "missing_fields": ["price"]},
            {"symbol": "H", "missing_fields": ["price"]},
            {"symbol": "K", "missing_fields": ["price"]},
            {"symbol": "L", "missing_fields": ["decision", "score", "price"]},
        ]

    def test_missing_decision_or_score_alone_never_enables_repair_count(self, m, env):
        rows = [rec(symbol="A", decision="", item_json='{"price": 5}'),
                rec(symbol="B", score=None, item_json='{"price": 5}')]
        out = _audit(env, m, rows)
        assert out["missing_data"] == 0 and out["missing_price"] == 0
        assert out["missing_decision"] == 1 and out["missing_score"] == 1
        assert len(out["incomplete_stocks"]) == 2

    def test_incomplete_list_is_capped_at_200_but_counts_are_not(self, m, env):
        rows = [rec(symbol=f"S{i}", item_json="{}") for i in range(250)]
        out = _audit(env, m, rows)
        assert len(out["incomplete_stocks"]) == 200
        assert out["incomplete_stocks"][0]["symbol"] == "S0"
        assert out["missing_price"] == 250 and out["missing_data"] == 250

    def test_health_score_100_when_everything_complete(self, m, env):
        out = _audit(env, m, [rec(symbol="A", item_json='{"price": 1}'), rec(symbol="B", item_json='{"close": 2}')])
        assert out["health_score"] == 100.0 and out["fully_populated"] == 2
        assert out["incomplete_stocks"] == []

    def test_section_label_case_is_ignored(self, m, env):
        out = _audit(env, m, [rec(section="NEWS_DRIVEN", item_json='{"price": 1}')])
        assert out["by_section"]["news_driven"] == 1

    def test_empty_table(self, m, env):
        out = _audit(env, m, [], max_row=(None,))
        assert out["ok"] is True and out["rows_24h"] == 0
        assert out["health_score"] == 0.0 and out["fully_populated"] == 0
        assert out["age_hours"] is None and out["fresh"] is False
        assert out["issues"] == ["Table exists but is empty — run a Hot Picks scan."]

    def test_no_max_row_is_treated_as_empty(self, m, env):
        out = _audit(env, m, [rec(item_json='{"price": 1}')], max_row=None)
        assert out["age_hours"] is None
        assert "Table exists but is empty — run a Hot Picks scan." in out["issues"]

    def test_fresh_rows_have_no_age_issue(self, m, env):
        out = _audit(env, m, [rec(item_json='{"price": 1}')])
        assert out["fresh"] is True
        assert out["age_hours"] == pytest.approx(2.0, abs=0.05)
        assert out["issues"] == []
        assert out["fresh_threshold_hours"] == 24.0 and out["retention_hours"] == 72.0

    def test_stale_rows_report_age_and_threshold(self, env):
        mod = env.load(HOTPICKS_DB_FRESH_HOURS="6")
        out = _audit(env, mod, [rec(item_json='{"price": 1}')], max_row=(NOW - timedelta(hours=10),))
        assert out["fresh"] is False and out["ok"] is True
        assert out["age_hours"] == pytest.approx(10.0, abs=0.05)
        assert len(out["issues"]) == 1
        assert out["issues"][0].startswith("Stored picks are 10.0h old (threshold 6.0h)")
        assert out["fresh_threshold_hours"] == 6.0

    def test_age_exactly_at_threshold_is_fresh(self, env, monkeypatch):
        mod = env.load()
        monkeypatch.setattr(mod, "_utc_hours_since", lambda _v: 24.0)
        out = _audit(env, mod, [rec(item_json='{"price": 1}')])
        assert out["fresh"] is True and out["issues"] == []

    def test_all_scores_missing_raises_decision_service_issue(self, m, env):
        rows = [rec(symbol="A", score=None, item_json='{"price": 1}'),
                rec(symbol="B", score=None, item_json='{"price": 1}')]
        out = _audit(env, m, rows)
        assert len(out["issues"]) == 1 and "missing a score" in out["issues"][0]
        assert "decision service" in out["issues"][0]

    def test_some_scores_missing_is_not_that_issue(self, m, env):
        rows = [rec(symbol="A", score=None, item_json='{"price": 1}'),
                rec(symbol="B", score=5, item_json='{"price": 1}')]
        assert _audit(env, m, rows)["issues"] == []

    def test_query_failure_reports_clipped_issue(self, m, env):
        env.hp.shared.raises_on = [("COUNT(*)", RuntimeError("z" * 400))]
        out = m._hotpicks_audit_uncached()
        assert out["ok"] is False
        assert out["issues"] == ["audit query failed: " + "z" * 160]

    def test_failure_after_partial_progress_keeps_ok_false(self, m, env):
        env.hp.shared.raises_on = [("MAX(updated_at)", RuntimeError("late boom"))]
        env.hp.shared.recent_keys = list(_RECENT_KEYS)
        env.hp.shared.recent_rows = [rec(item_json='{"price": 1}')]
        out = m._hotpicks_audit_uncached()
        assert out["ok"] is False and out["issues"] == ["audit query failed: late boom"]


# ── ensure_hotpicks_schema ────────────────────────────────────────────────────

class TestEnsureSchema:
    def test_delegates(self, m, env):
        env.hp.ensure_result = {"ok": True, "created": ["t"]}
        assert m.ensure_hotpicks_schema() == {"ok": True, "created": ["t"]}

    def test_error_is_clipped(self, m, env):
        env.hp.ensure_raises = RuntimeError("e" * 500)
        out = m.ensure_hotpicks_schema()
        assert out == {"ok": False, "error": "e" * 200}

    def test_schema_missing(self, env):
        out = env.load(no_schema=True).ensure_hotpicks_schema()
        assert out["ok"] is False and out["error"]


# ── _row_needs_scores ─────────────────────────────────────────────────────────

_FULL = {"technical_score": 60, "combined_score": 70, "entry_range": "100-105", "target": 120}


class TestRowNeedsScores:
    def test_complete_row_does_not_need_scores(self, m):
        assert m._row_needs_scores(dict(_FULL)) is False

    @pytest.mark.parametrize("bad", [None, [], "x", 5, ()])
    def test_non_dict_needs_scores(self, m, bad):
        assert m._row_needs_scores(bad) is True

    def test_missing_or_zero_technical_score(self, m):
        assert m._row_needs_scores({k: v for k, v in _FULL.items() if k != "technical_score"}) is True
        assert m._row_needs_scores(dict(_FULL, technical_score=0)) is True

    def test_combined_or_score_required(self, m):
        no_comb = {k: v for k, v in _FULL.items() if k != "combined_score"}
        assert m._row_needs_scores(no_comb) is True
        assert m._row_needs_scores(dict(no_comb, score=55)) is False
        assert m._row_needs_scores(dict(no_comb, score=0)) is True

    def test_entry_or_target_required(self, m):
        base = {"technical_score": 60, "combined_score": 70}
        assert m._row_needs_scores(base) is True
        assert m._row_needs_scores(dict(base, entry_range="1-2")) is False
        assert m._row_needs_scores(dict(base, target=5)) is False
        assert m._row_needs_scores(dict(base, entry_range="", target=0)) is True

    def test_empty_dict(self, m):
        assert m._row_needs_scores({}) is True


# ── hotpicks_repair_batch (price fill) ────────────────────────────────────────

MD = "http://md:8001"


def _targets(env, *rows):
    env.hp.shared.target_rows = list(rows)


def _json(**kw):
    return json.dumps(kw)


class TestRepairBatchGuards:
    def test_schema_unavailable(self, env):
        out = env.load(no_schema=True).hotpicks_repair_batch()
        assert out["status"] == "error" and out["error"].startswith("hotpicks_schema unavailable: ")
        assert out["repaired"] == [] and out["attempted"] == 0

    def test_sqlalchemy_unavailable(self, env):
        assert env.load(no_sa=True).hotpicks_repair_batch()["status"] == "error"

    def test_not_configured(self, m, env):
        env.hp.url = None
        out = m.hotpicks_repair_batch()
        assert out == {"status": "not_configured", "repaired": [], "attempted": 0}

    def test_engine_none(self, m, env):
        env.hp.shared_none = True
        out = m.hotpicks_repair_batch()
        assert out["status"] == "error" and out["error"] == "Could not build a database engine"

    def test_outer_failure_is_reported_and_clipped(self, m, env, caplog):
        env.hp.shared.raises_on = [("SELECT symbol", RuntimeError("q" * 400))]
        with caplog.at_level(logging.WARNING, logger="hotpicks-store"):
            out = m.hotpicks_repair_batch()
        assert out["status"] == "error" and out["error"] == "q" * 200
        assert any("hotpicks_repair_batch failed" in r.getMessage() for r in caplog.records)


class TestRepairBatchSelection:
    def test_select_sql_is_24h_and_dialect_specific(self, m, env):
        m.hotpicks_repair_batch()
        pg = env.hp.shared.find("SELECT symbol")[0][0]
        assert pg == ("SELECT symbol, section, item_json FROM hotpicks_static_feed "
                      "WHERE updated_at >= NOW() - INTERVAL '24 hours'")
        env.hp.shared.calls.clear()
        env.hp.dial = "oracle"
        m.hotpicks_repair_batch()
        ora = env.hp.shared.find("SELECT symbol")[0][0]
        assert ora.endswith("WHERE updated_at >= SYSTIMESTAMP - NUMTODSINTERVAL(24, 'HOUR')")
        assert env.hp.shared_apps[0] == "stockky-hotpicks-repair"

    def test_nothing_missing_a_price(self, m, env):
        _targets(env, ("A", "news_driven", _json(price=10)), ("B", "news_driven", _json(close=5)))
        out = m.hotpicks_repair_batch()
        assert out == {"status": "completed", "repaired": [], "attempted": 0,
                       "message": "Nothing missing a price."}
        assert env.http.gets == [] and env.http.client_kwargs == []

    def test_empty_table_is_nothing_missing(self, m, env):
        assert m.hotpicks_repair_batch()["message"] == "Nothing missing a price."

    @pytest.mark.parametrize("item_json, needs_repair", [
        (None, True), ("", True), ("{}", True), ("not json", True),
        (_json(price=0, close=0), True), (_json(price=-5), True), (_json(price=None), True),
        (_json(price="abc", close=5), False),          # bad price value falls through to close
        (_json(price="abc"), True),
        (_json(price=10), False), (_json(close=10), False), (_json(price=0, close=3), False),
    ])
    def test_which_rows_are_targets(self, m, env, item_json, needs_repair):
        _targets(env, ("A", "news_driven", item_json))
        out = m.hotpicks_repair_batch()
        assert out["attempted"] == (1 if needs_repair else 0)

    def test_force_symbol_normalisation_and_filter(self, m, env):
        _targets(env, ("RELIANCE", "news_driven", "{}"), ("TCS", "news_driven", "{}"))
        env.http.routes[f"{MD}/quote/RELIANCE"] = FakeResp(200, {"price": 2500})
        out = m.hotpicks_repair_batch(symbol=" reliance.ns ", market_data_url=MD)
        assert out["attempted"] == 1 and out["repaired"] == ["RELIANCE"]
        assert env.http.gets == [f"{MD}/quote/RELIANCE"]
        out = m.hotpicks_repair_batch(symbol="reliance.bo", market_data_url=MD)
        assert out["repaired"] == ["RELIANCE"]

    @pytest.mark.parametrize("blank", ["", "   ", ".NS", None])
    def test_blank_symbol_means_no_filter(self, m, env, blank):
        _targets(env, ("A", "news_driven", "{}"), ("B", "news_driven", "{}"))
        assert m.hotpicks_repair_batch(symbol=blank, market_data_url=MD)["attempted"] == 2

    def test_force_symbol_not_in_window(self, m, env):
        _targets(env, ("TCS", "news_driven", "{}"))
        out = m.hotpicks_repair_batch(symbol="nope")
        assert out["status"] == "not_found"
        assert out["message"] == "NOPE not in the last 24h of stored Hot Picks."
        assert env.http.gets == []

    def test_force_symbol_that_already_has_a_price_is_not_found(self, m, env):
        _targets(env, ("TCS", "news_driven", _json(price=100)))
        assert m.hotpicks_repair_batch(symbol="TCS")["status"] == "not_found"

    @pytest.mark.parametrize("limit, expected", [(None, 15), (0, 15), (3, 3), (-5, 1), (1, 1),
                                                  (100, 100), (1000, 100)])
    def test_limit_clamp(self, m, env, limit, expected):
        _targets(env, *[(f"S{i}", "news_driven", "{}") for i in range(120)])
        out = m.hotpicks_repair_batch(limit=limit, market_data_url=MD)
        assert out["attempted"] == expected == len(env.http.gets)

    def test_limit_keeps_first_rows_in_order(self, m, env):
        _targets(env, *[(f"S{i}", "news_driven", "{}") for i in range(5)])
        m.hotpicks_repair_batch(limit=2, market_data_url=MD)
        assert env.http.gets == [f"{MD}/quote/S0", f"{MD}/quote/S1"]


class TestRepairBatchFetch:
    def _one(self, env, item_json="{}"):
        _targets(env, ("AAA", "news_driven", item_json))

    def test_client_settings(self, m, env):
        self._one(env)
        m.hotpicks_repair_batch(market_data_url=MD)
        assert env.http.client_kwargs == [{"timeout": 8.0, "follow_redirects": True}]

    def test_url_precedence_and_trailing_slash(self, env):
        mod = env.load(MARKET_DATA_URL="http://envmd/")
        self._one(env)
        mod.hotpicks_repair_batch()
        assert env.http.gets == ["http://envmd/quote/AAA"]
        env.http.gets.clear()
        mod.hotpicks_repair_batch(market_data_url="http://arg///")
        assert env.http.gets == ["http://arg/quote/AAA"]

    def test_no_url_at_all_uses_empty_base(self, m, env):
        self._one(env)
        m.hotpicks_repair_batch()
        assert env.http.gets == ["/quote/AAA"]

    def test_successful_update(self, m, env):
        _targets(env, ("AAA", "results_driven", _json(decision="BUY", keep="me")))
        env.http.routes[f"{MD}/quote/AAA"] = FakeResp(200, {"price": "123.5"})
        out = m.hotpicks_repair_batch(market_data_url=MD)
        assert out == {"status": "completed", "repaired": ["AAA"], "attempted": 1}
        ((sql, params),) = env.hp.shared.find("UPDATE")
        assert sql == ("UPDATE hotpicks_static_feed SET item_json = :item_json, "
                       "updated_at = NOW[postgresql] WHERE symbol = :symbol AND section = :section")
        assert params["symbol"] == "AAA" and params["section"] == "results_driven"
        assert json.loads(params["item_json"]) == {"decision": "BUY", "keep": "me", "price": 123.5, "close": 123.5}
        assert env.hp.shared.begins == 1

    def test_update_uses_dialect_now_func(self, m, env):
        env.hp.dial = "oracle"
        self._one(env)
        env.http.routes[f"{MD}/quote/AAA"] = FakeResp(200, {"price": 5})
        m.hotpicks_repair_batch(market_data_url=MD)
        assert "updated_at = NOW[oracle]" in env.hp.shared.find("UPDATE")[0][0]

    def test_item_json_is_capped_at_15000_chars(self, m, env):
        _targets(env, ("AAA", "news_driven", _json(blob="x" * 20000)))
        env.http.routes[f"{MD}/quote/AAA"] = FakeResp(200, {"price": 5})
        m.hotpicks_repair_batch(market_data_url=MD)
        assert len(env.hp.shared.find("UPDATE")[0][1]["item_json"]) == 15000

    def test_price_key_priority(self, m, env):
        self._one(env)
        env.http.routes[f"{MD}/quote/AAA"] = FakeResp(
            200, {"last_price": 5, "close": 4, "ltp": 3, "cmp": 2, "price": 1})
        m.hotpicks_repair_batch(market_data_url=MD)
        assert json.loads(env.hp.shared.find("UPDATE")[0][1]["item_json"])["price"] == 1.0

    @pytest.mark.parametrize("body, expected", [
        ({"cmp": 2}, 2.0), ({"ltp": 3}, 3.0), ({"close": 4}, 4.0), ({"last_price": 5}, 5.0),
        ({"price": 0, "cmp": 9}, 9.0), ({"price": None, "ltp": 8}, 8.0),
        ({"price": "bad", "close": 6}, 6.0), ({"price": [], "close": 6}, 6.0),
        ({"price": -3, "last_price": 7}, 7.0),
    ])
    def test_price_key_fallthrough(self, m, env, body, expected):
        self._one(env)
        env.http.routes[f"{MD}/quote/AAA"] = FakeResp(200, body)
        assert m.hotpicks_repair_batch(market_data_url=MD)["repaired"] == ["AAA"]
        assert json.loads(env.hp.shared.find("UPDATE")[0][1]["item_json"])["price"] == expected

    @pytest.mark.parametrize("resp", [
        FakeResp(500, {"price": 5}), FakeResp(404, {}), FakeResp(200, {}), FakeResp(200, []),
        FakeResp(200, "text"), FakeResp(200, {"price": 0}), FakeResp(200, {"price": "abc"}),
        FakeResp(200, {"unrelated": 1}),
    ])
    def test_unusable_responses_leave_the_row_alone(self, m, env, resp):
        self._one(env)
        env.http.routes[f"{MD}/quote/AAA"] = resp
        out = m.hotpicks_repair_batch(market_data_url=MD)
        assert out["repaired"] == [] and out["attempted"] == 1
        assert env.hp.shared.find("UPDATE") == []

    def test_non_200_does_not_sleep_but_no_price_does(self, m, env):
        _targets(env, ("A", "news_driven", "{}"), ("B", "news_driven", "{}"))
        env.http.routes[f"{MD}/quote/A"] = FakeResp(500, {})
        env.http.routes[f"{MD}/quote/B"] = FakeResp(200, {})       # 200 but no price -> 0.5s sleep + continue
        m.hotpicks_repair_batch(market_data_url=MD)
        assert env.clock.slept == [0.5]

    def test_successful_repair_sleeps_half_a_second(self, m, env):
        self._one(env)
        env.http.routes[f"{MD}/quote/AAA"] = FakeResp(200, {"price": 5})
        m.hotpicks_repair_batch(market_data_url=MD)
        assert env.clock.slept == [0.5]

    def test_request_exception_is_isolated_per_symbol(self, m, env, caplog):
        _targets(env, ("BAD", "news_driven", "{}"), ("GOOD", "news_driven", "{}"))
        env.http.routes[f"{MD}/quote/BAD"] = RuntimeError("net boom")
        env.http.routes[f"{MD}/quote/GOOD"] = FakeResp(200, {"price": 9})
        with caplog.at_level(logging.DEBUG, logger="hotpicks-store"):
            out = m.hotpicks_repair_batch(market_data_url=MD)
        assert out["repaired"] == ["GOOD"] and out["attempted"] == 2
        assert any("hotpicks repair BAD failed" in r.getMessage() for r in caplog.records)
        assert env.clock.slept == [0.5, 0.5]

    def test_json_decode_error_is_isolated(self, m, env):
        self._one(env)
        env.http.routes[f"{MD}/quote/AAA"] = FakeResp(200, ValueError("bad json"))
        assert m.hotpicks_repair_batch(market_data_url=MD)["repaired"] == []

    @pytest.mark.parametrize("cap, px, ok", [
        (None, 5000, True), ("0", 5000, True), ("", 5000, True),
        ("100", 150, False), ("100", 100, True), ("100", 99.5, True),
    ])
    def test_max_stock_price_gate(self, env, cap, px, ok):
        mod = env.load(**({"MAX_STOCK_PRICE": cap} if cap is not None else {}))
        self._one(env)
        env.http.routes[f"{MD}/quote/AAA"] = FakeResp(200, {"price": px})
        out = mod.hotpicks_repair_batch(market_data_url=MD)
        assert out["repaired"] == (["AAA"] if ok else [])

    def test_over_cap_symbol_sleeps_once_only(self, env):
        mod = env.load(MAX_STOCK_PRICE="10")
        self._one(env)
        env.http.routes[f"{MD}/quote/AAA"] = FakeResp(200, {"price": 50})
        mod.hotpicks_repair_batch(market_data_url=MD)
        assert env.clock.slept == [0.5]

    def test_audit_cache_is_invalidated(self, m, env):
        m._AUDIT_CACHE["v"] = (1.0, {"ok": True})
        self._one(env)
        m.hotpicks_repair_batch(market_data_url=MD)
        assert "v" not in m._AUDIT_CACHE

    def test_audit_cache_is_kept_when_nothing_to_do(self, m, env):
        m._AUDIT_CACHE["v"] = (1.0, {"ok": True})
        m.hotpicks_repair_batch(market_data_url=MD)                # early return: nothing missing
        assert "v" in m._AUDIT_CACHE

    def test_two_rows_same_symbol_different_sections_are_updated_separately(self, m, env):
        _targets(env, ("AAA", "news_driven", "{}"), ("AAA", "results_driven", "{}"))
        env.http.routes[f"{MD}/quote/AAA"] = FakeResp(200, {"price": 5})
        out = m.hotpicks_repair_batch(market_data_url=MD)
        assert out["repaired"] == ["AAA", "AAA"]
        assert [c[1]["section"] for c in env.hp.shared.find("UPDATE")] == ["news_driven", "results_driven"]


# ── hotpicks_repair_scores (decision fill) ────────────────────────────────────

DEC = "http://dec:8003"


def _blob(**kw):
    return json.dumps(kw)


def _needs(**kw):
    """A blob that needs scores (no technical_score)."""
    return json.dumps(dict({"price": 10}, **kw))


class TestRepairScoresGuards:
    def test_decision_url_required_before_anything_else(self, m, env):
        out = m.hotpicks_repair_scores()
        assert out == {"status": "not_configured", "repaired": [], "attempted": 0,
                       "error": "DECISION_URL not set"}
        assert env.hp.shared_apps == []

    def test_decision_url_from_env(self, env):
        mod = env.load(DECISION_URL="http://envdec/")
        env.hp.shared.target_rows = [("A", "news_driven", _needs())]
        mod.hotpicks_repair_scores()
        assert env.http.gets == ["http://envdec/decide/A"]

    def test_decision_url_argument_wins_and_is_stripped(self, env):
        mod = env.load(DECISION_URL="http://envdec")
        env.hp.shared.target_rows = [("A", "news_driven", _needs())]
        mod.hotpicks_repair_scores(decision_url="http://arg//")
        assert env.http.gets == ["http://arg/decide/A"]

    def test_schema_unavailable(self, env):
        out = env.load(no_schema=True).hotpicks_repair_scores(decision_url=DEC)
        assert out["status"] == "error" and out["error"].startswith("hotpicks_schema unavailable: ")

    def test_sqlalchemy_unavailable(self, env):
        assert env.load(no_sa=True).hotpicks_repair_scores(decision_url=DEC)["status"] == "error"

    def test_database_not_configured(self, m, env):
        env.hp.url = None
        out = m.hotpicks_repair_scores(decision_url=DEC)
        assert out == {"status": "not_configured", "repaired": [], "attempted": 0}

    def test_engine_none(self, m, env):
        env.hp.shared_none = True
        out = m.hotpicks_repair_scores(decision_url=DEC)
        assert out["status"] == "error" and out["error"] == "Could not build a database engine"

    def test_outer_failure(self, m, env, caplog):
        env.hp.shared.raises_on = [("SELECT symbol", RuntimeError("w" * 400))]
        with caplog.at_level(logging.WARNING, logger="hotpicks-store"):
            out = m.hotpicks_repair_scores(decision_url=DEC)
        assert out["status"] == "error" and out["error"] == "w" * 200
        assert any("hotpicks_repair_scores failed" in r.getMessage() for r in caplog.records)


class TestRepairScoresSelection:
    def test_select_sql_is_72h_and_dialect_specific(self, m, env):
        m.hotpicks_repair_scores(decision_url=DEC)
        assert env.hp.shared.find("SELECT symbol")[0][0] == (
            "SELECT symbol, section, item_json FROM hotpicks_static_feed "
            "WHERE updated_at >= NOW() - INTERVAL '72 hours'")
        env.hp.shared.calls.clear()
        env.hp.dial = "oracle"
        m.hotpicks_repair_scores(decision_url=DEC)
        assert env.hp.shared.find("SELECT symbol")[0][0].endswith(
            "WHERE updated_at >= SYSTIMESTAMP - NUMTODSINTERVAL(72, 'HOUR')")
        assert env.hp.shared_apps[0] == "stockky-hotpicks-score-repair"

    def test_only_rows_needing_scores_are_targeted(self, m, env):
        env.hp.shared.target_rows = [
            ("FULL", "news_driven", json.dumps(_FULL)),
            ("NEED1", "news_driven", _needs()),
            ("BADJSON", "results_driven", "not json"),
            ("NONE", "bulk_insider_driven", None),
        ]
        m.hotpicks_repair_scores(decision_url=DEC)
        assert env.http.gets == [f"{DEC}/decide/NEED1", f"{DEC}/decide/BADJSON", f"{DEC}/decide/NONE"]

    def test_completed_with_nothing_to_repair(self, m, env):
        env.hp.shared.target_rows = [("FULL", "news_driven", json.dumps(_FULL))]
        out = m.hotpicks_repair_scores(decision_url=DEC)
        assert out["status"] == "completed" and out["repaired"] == []
        assert env.http.gets == []

    def test_force_symbol_normalises_and_filters(self, m, env):
        env.hp.shared.target_rows = [("RELIANCE", "news_driven", _needs()), ("TCS", "news_driven", _needs())]
        env.http.routes[f"{DEC}/decide/RELIANCE"] = FakeResp(200, {"decision": "BUY"})
        out = m.hotpicks_repair_scores(symbol=" reliance.ns ", decision_url=DEC)
        assert out["repaired"] == ["RELIANCE"] and env.http.gets == [f"{DEC}/decide/RELIANCE"]
        out = m.hotpicks_repair_scores(symbol="reliance.bo", decision_url=DEC)
        assert out["repaired"] == ["RELIANCE"]

    @pytest.mark.parametrize("blank", ["", "  ", ".NS", None])
    def test_blank_symbol_means_no_filter(self, m, env, blank):
        env.hp.shared.target_rows = [("A", "news_driven", _needs()), ("B", "news_driven", _needs())]
        m.hotpicks_repair_scores(symbol=blank, decision_url=DEC)
        assert len(env.http.gets) == 2

    def test_force_symbol_not_missing_scores(self, m, env):
        env.hp.shared.target_rows = [("TCS", "news_driven", json.dumps(_FULL))]
        out = m.hotpicks_repair_scores(symbol="tcs", decision_url=DEC)
        assert out["status"] == "not_found"
        assert out["message"] == "TCS not missing any scores in the last 72h."
        assert env.http.gets == []

    @pytest.mark.parametrize("limit, expected", [(None, 15), (0, 15), (3, 3), (-5, 1), (100, 100), (1000, 100)])
    def test_limit_clamp(self, m, env, limit, expected):
        env.hp.shared.target_rows = [(f"S{i}", "news_driven", _needs()) for i in range(120)]
        m.hotpicks_repair_scores(limit=limit, decision_url=DEC)
        assert len(env.http.gets) == expected


class TestRepairScoresFetch:
    def _one(self, env, blob=None, section="news_driven"):
        env.hp.shared.target_rows = [("AAA", section, blob if blob is not None else _needs())]

    def _saved(self, env):
        ((sql, params),) = env.hp.shared.find("UPDATE")
        return sql, params, json.loads(params["item_json"])

    def test_client_settings(self, m, env):
        self._one(env)
        m.hotpicks_repair_scores(decision_url=DEC)
        assert env.http.client_kwargs == [{"timeout": 15.0, "follow_redirects": True}]

    def test_copies_decision_scores_and_levels(self, m, env):
        self._one(env, _needs(keep="me"), section="bulk_insider_driven")
        env.http.routes[f"{DEC}/decide/AAA"] = FakeResp(200, {
            "decision": "BUY", "combined_score": 77, "technical_score": 61, "fundamental_score": 55,
            "news_score": 40, "prediction_score": 52, "market_score": 48, "training_score": 50,
            "entry_range": "100-105", "target": 130, "stop_loss": 95, "holding_period": "5d",
            "holding_period_estimate": "3-7d", "confidence": "HIGH", "reasons": ["r1", "r2"],
            "ignored_field": "nope"})
        out = m.hotpicks_repair_scores(decision_url=DEC)
        assert out == {"status": "completed", "repaired": ["AAA"], "attempted": 0}
        sql, params, blob = self._saved(env)
        assert sql == ("UPDATE hotpicks_static_feed SET item_json = :item_json, "
                       "updated_at = NOW[postgresql] WHERE symbol = :symbol AND section = :section")
        assert params["symbol"] == "AAA" and params["section"] == "bulk_insider_driven"
        assert blob == {
            "price": 10, "keep": "me", "decision": "BUY", "score": 77, "combined_score": 77,
            "technical_score": 61, "fundamental_score": 55, "news_score": 40, "prediction_score": 52,
            "market_score": 48, "training_score": 50, "entry_range": "100-105", "target": 130,
            "stop_loss": 95, "holding_period": "5d", "holding_period_estimate": "3-7d",
            "confidence": "HIGH", "reasons": ["r1", "r2"]}

    def test_update_uses_dialect_now_func(self, m, env):
        env.hp.dial = "oracle"
        self._one(env)
        env.http.routes[f"{DEC}/decide/AAA"] = FakeResp(200, {"decision": "BUY"})
        m.hotpicks_repair_scores(decision_url=DEC)
        assert "updated_at = NOW[oracle]" in self._saved(env)[0]

    def test_zero_combined_score_is_copied(self, m, env):
        self._one(env)
        env.http.routes[f"{DEC}/decide/AAA"] = FakeResp(200, {"combined_score": 0})
        assert m.hotpicks_repair_scores(decision_url=DEC)["repaired"] == ["AAA"]
        blob = self._saved(env)[2]
        assert blob["score"] == 0 and blob["combined_score"] == 0

    def test_na_and_none_fields_are_not_copied(self, m, env):
        self._one(env, _needs(technical_score=None, target="old"))
        env.http.routes[f"{DEC}/decide/AAA"] = FakeResp(200, {
            "decision": "HOLD", "technical_score": "N/A", "target": None, "stop_loss": "N/A",
            "confidence": "LOW"})
        m.hotpicks_repair_scores(decision_url=DEC)
        blob = self._saved(env)[2]
        assert blob["technical_score"] is None and blob["target"] == "old"
        assert "stop_loss" not in blob
        assert blob["confidence"] == "LOW" and blob["decision"] == "HOLD"

    def test_falsy_decision_is_not_copied(self, m, env):
        self._one(env, _needs(decision="OLD"))
        env.http.routes[f"{DEC}/decide/AAA"] = FakeResp(200, {"decision": "", "confidence": "LOW"})
        m.hotpicks_repair_scores(decision_url=DEC)
        assert self._saved(env)[2]["decision"] == "OLD"

    def test_reasons_only_replaced_by_a_list(self, m, env):
        self._one(env, _needs(reasons=["old"]))
        env.http.routes[f"{DEC}/decide/AAA"] = FakeResp(200, {"decision": "BUY", "reasons": "a string"})
        m.hotpicks_repair_scores(decision_url=DEC)
        assert self._saved(env)[2]["reasons"] == ["old"]

    def test_empty_reasons_list_is_ignored(self, m, env):
        self._one(env, _needs(reasons=["old"]))
        env.http.routes[f"{DEC}/decide/AAA"] = FakeResp(200, {"decision": "BUY", "reasons": []})
        m.hotpicks_repair_scores(decision_url=DEC)
        assert self._saved(env)[2]["reasons"] == ["old"]

    def test_nothing_useful_in_body_means_no_write(self, m, env):
        self._one(env)
        env.http.routes[f"{DEC}/decide/AAA"] = FakeResp(200, {"foo": "bar", "reasons": ["only reasons"]})
        out = m.hotpicks_repair_scores(decision_url=DEC)
        assert out["repaired"] == [] and env.hp.shared.find("UPDATE") == []
        assert env.clock.slept == [0.4]

    def test_item_json_is_capped_at_15000_chars(self, m, env):
        self._one(env, _needs(blob="x" * 20000))
        env.http.routes[f"{DEC}/decide/AAA"] = FakeResp(200, {"decision": "BUY"})
        m.hotpicks_repair_scores(decision_url=DEC)
        assert len(env.hp.shared.find("UPDATE")[0][1]["item_json"]) == 15000

    @pytest.mark.parametrize("resp", [FakeResp(500, {"decision": "BUY"}), FakeResp(404, {})])
    def test_non_200_sleeps_and_skips(self, m, env, resp):
        self._one(env)
        env.http.routes[f"{DEC}/decide/AAA"] = resp
        out = m.hotpicks_repair_scores(decision_url=DEC)
        assert out["repaired"] == [] and env.clock.slept == [0.4]
        assert env.hp.shared.find("UPDATE") == []

    @pytest.mark.parametrize("body", [{}, [], "text", None])
    def test_empty_or_non_dict_body_sleeps_and_skips(self, m, env, body):
        self._one(env)
        env.http.routes[f"{DEC}/decide/AAA"] = FakeResp(200, body)
        assert m.hotpicks_repair_scores(decision_url=DEC)["repaired"] == []
        assert env.clock.slept == [0.4]

    def test_successful_repair_sleeps_after(self, m, env):
        self._one(env)
        env.http.routes[f"{DEC}/decide/AAA"] = FakeResp(200, {"decision": "BUY"})
        m.hotpicks_repair_scores(decision_url=DEC)
        assert env.clock.slept == [0.4]

    def test_request_exception_is_isolated_per_symbol(self, m, env, caplog):
        env.hp.shared.target_rows = [("BAD", "news_driven", _needs()), ("GOOD", "news_driven", _needs())]
        env.http.routes[f"{DEC}/decide/BAD"] = RuntimeError("net boom")
        env.http.routes[f"{DEC}/decide/GOOD"] = FakeResp(200, {"decision": "BUY"})
        with caplog.at_level(logging.DEBUG, logger="hotpicks-store"):
            out = m.hotpicks_repair_scores(decision_url=DEC)
        assert out["repaired"] == ["GOOD"]
        assert any("hotpicks score-repair BAD failed" in r.getMessage() for r in caplog.records)
        assert env.clock.slept == [0.4, 0.4]

    def test_audit_cache_is_invalidated(self, m, env):
        m._AUDIT_CACHE["v"] = (1.0, {"ok": True})
        self._one(env)
        m.hotpicks_repair_scores(decision_url=DEC)
        assert "v" not in m._AUDIT_CACHE

    def test_audit_cache_is_kept_on_early_return(self, m, env):
        m._AUDIT_CACHE["v"] = (1.0, {"ok": True})
        m.hotpicks_repair_scores(symbol="ghost", decision_url=DEC)     # not_found path
        assert "v" in m._AUDIT_CACHE

    def test_same_symbol_two_sections_updated_separately(self, m, env):
        env.hp.shared.target_rows = [("AAA", "news_driven", _needs()), ("AAA", "results_driven", _needs())]
        env.http.routes[f"{DEC}/decide/AAA"] = FakeResp(200, {"decision": "BUY"})
        out = m.hotpicks_repair_scores(decision_url=DEC)
        assert out["repaired"] == ["AAA", "AAA"]
        assert [c[1]["section"] for c in env.hp.shared.find("UPDATE")] == ["news_driven", "results_driven"]


# ── through the REAL hotpicks_schema + drift guards ──────────────────────────

class RealBacked:
    """hotpicks_schema for real (SQL text, ROW_KEYS, adapt_rows, coerce_bool …); only the engine
    factories and the DB url/dialect are overridden so nothing touches a database."""

    def __init__(self, real, eng, dial="postgresql"):
        self._real = real
        self.eng = eng
        self.dial = dial

    def __getattr__(self, name):
        return getattr(self._real, name)

    def database_url(self):
        return "postgresql://fake"

    def dialect(self):
        return self.dial

    def make_engine(self, app="x"):
        return self.eng

    def shared_engine(self, app="x"):
        return self.eng


@pytest.fixture(scope="module")
def real_schema():
    spec = importlib.util.spec_from_file_location("hotpicks_schema_real_for_store_tests", _SCHEMA_PATH)
    mod = importlib.util.module_from_spec(spec)
    saved = {k: sys.modules.get(k) for k in ("oracle_compat", "sqlalchemy")}
    sys.modules["oracle_compat"] = None
    sys.modules["sqlalchemy"] = _fake_sqlalchemy()
    try:
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return mod


def _load_with(env, hp):
    env.mp.setitem(sys.modules, "hotpicks_schema", hp)
    env.mp.setitem(sys.modules, "sqlalchemy", _fake_sqlalchemy())
    env.mp.setitem(sys.modules, "httpx", env.http.module())
    spec = importlib.util.spec_from_file_location("hotpicks_store_real_schema", _STORE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.time = env.clock
    return mod


class TestAgainstRealSchema:
    def test_every_schema_attribute_the_store_uses_exists(self, real_schema):
        tree = ast.parse(open(_STORE_PATH, encoding="utf-8").read())
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "hp":
                used.add(node.attr)
        # the store also calls _schema().ensure_hotpicks_schema() directly
        used.add("ensure_hotpicks_schema")
        assert {"ROW_KEYS", "TABLE_NAME", "database_url", "dialect", "make_engine", "shared_engine",
                "adapt_rows", "upsert_sql", "table_exists_sql", "select_recent_sql",
                "delete_older_than_sql", "now_func", "coerce_bool"} <= used
        missing = sorted(n for n in used if not hasattr(real_schema, n))
        assert missing == []

    def test_payload_rows_emit_exactly_row_keys(self, env, real_schema):
        mod = _load_with(env, RealBacked(real_schema, FakeEngine()))
        rows = mod._payload_rows({"generated_at": "g", "news_driven": [{"symbol": "a"}]})
        assert set(rows[0]) == set(real_schema.ROW_KEYS)

    def test_columns_the_store_reads_exist_in_select_columns(self, real_schema):
        cols = {c.strip() for c in real_schema.SELECT_COLUMNS.split(",")}
        read_by_row_to_item = {"item_json", "symbol", "section", "decision", "score", "news_score",
                               "headline_count", "signal_strength", "summary", "next_earnings_date",
                               "from_scan", "updated_at", "generated_at"}
        assert read_by_row_to_item <= cols

    def test_columns_the_audit_and_repairs_touch_exist_in_the_ddl(self, real_schema):
        ddl = " ".join(real_schema.ddl_statements("postgresql")).lower()
        for col in ("symbol", "section", "item_json", "updated_at", "decision", "score"):
            assert col in ddl
        assert real_schema.TABLE_NAME in ddl

    def test_sections_match_the_schema_docs(self, m):
        # SECTIONS is the contract with main.py's /stockky-hot payload keys
        assert m.SECTIONS == ("news_driven", "results_driven", "bulk_insider_driven")

    @pytest.mark.parametrize("dial", ["postgresql", "oracle"])
    def test_write_then_read_round_trip(self, env, real_schema, dial):
        """payload -> _payload_rows -> real adapt_rows -> (pretend DB) -> _row_to_item."""
        eng = FakeEngine()
        hp = RealBacked(real_schema, eng, dial)
        mod = _load_with(env, hp)
        item = {"symbol": "reliance", "decision": "BUY", "score": 71.5, "news_score": 4,
                "headline_count": 6, "signal_strength": "strong", "from_scan": True,
                "next_earnings_date": "2026-11-05", "summary": "Good quarter — ₹ résumé",
                "price": 2500.0, "reasons": ["a", "b"]}
        assert mod.hotpicks_db_upsert({"generated_at": "2026-09-30T10:00:00", "news_driven": [item]}) == 1

        upsert_calls = [c for c in eng.calls if "MERGE" in c[0].upper() or "INSERT" in c[0].upper()]
        assert upsert_calls, eng.sqls()
        params = upsert_calls[0][1]
        row = params[0] if isinstance(params, list) else params
        assert set(row) >= set(real_schema.ROW_KEYS)
        assert row["symbol"] == "RELIANCE"
        assert row["from_scan"] == (1 if dial == "oracle" else True)

        stored = dict(row, updated_at=NOW - timedelta(hours=1))
        stored["from_scan"] = row["from_scan"]
        built = mod._row_to_item(stored, hp)
        assert built["symbol"] == "RELIANCE" or built["symbol"] == "reliance"
        assert built["price"] == 2500.0 and built["reasons"] == ["a", "b"]
        assert built["from_scan"] is True
        assert built["summary"] == "Good quarter — ₹ résumé"
        assert built["stored_generated_at"] == "2026-09-30T10:00:00"

    def test_read_path_with_real_sql_builders(self, env, real_schema):
        """The store's reader driven with the REAL exists/select SQL strings."""
        eng = FakeEngine()
        hp = RealBacked(real_schema, eng)
        mod = _load_with(env, hp)
        eng.answers = {
            real_schema.table_exists_sql("postgresql"): FakeResult(scalar=True),
            real_schema.select_recent_sql("postgresql"): FakeResult(keys=_RECENT_KEYS, rows=[rec()]),
        }
        out = mod.hotpicks_db_payload()
        assert out is not None and out["count"] == 1 and out["backend"] == "postgresql"
        assert eng.calls[0][1] == {"tbl": real_schema.TABLE_NAME}
        assert eng.calls[1][1] == {"hours": 24}
