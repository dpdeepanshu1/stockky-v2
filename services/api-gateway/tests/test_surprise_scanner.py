"""tests/test_surprise_scanner.py — coverage for api-gateway/surprise_scanner.py

The intraday "Surprise Stock" engine: env-driven thresholds, DB URL / engine / dialect plumbing,
the static-baseline cache (load + KV seed), the 100-point score_stock() model and its tiers, the
quote fetchers, the chunked scan() with sector sympathy and the durable last-result cache, and the
market-aware feed / audit / repair helpers.

No network, no database, no real yfinance. Every test loads a FRESH copy of the module (import-time
env never leaks between tests). `kv_cache`, `data_feed`, `surprise_schema`, `nse_holidays`,
`zoneinfo`, `datetime` and `yfinance` are small fakes in sys.modules; the DB is a fake engine that
records every statement. Clocks are fake and nothing sleeps.

Run from services/api-gateway:
    python3 -m pytest tests/test_surprise_scanner.py -v --cov=surprise_scanner --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import datetime as _real_dt
import decimal
import importlib.util
import json
import logging
import os
import sys
import types
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
_MOD_PATH = os.path.join(_SERVICE, "surprise_scanner.py")

_ENV_KEYS = (
    "SURPRISE_MIN_SCORE", "HARD_FLOOR_LIQUIDITY", "SURPRISE_MIN_CHANGE_PCT", "SURPRISE_SCAN_CONCURRENCY",
    "SURPRISE_QUOTE_TIMEOUT", "SURPRISE_CACHE_MAX_AGE_SEC", "MAX_STOCK_PRICE", "VALUE_BUY_THRESHOLD",
    "SURPRISE_BUILDING_MIN_SCORE", "SURPRISE_BUILDING_MIN_CHANGE_PCT", "SURPRISE_RVOL_SLOPE_MIN",
    "SURPRISE_SECTOR_SYMPATHY_MIN_SCORE", "SURPRISE_DIST_52W_BREAKOUT_PCT", "SURPRISE_DIST_52W_NEAR_PCT",
    "SURPRISE_SHOCKER_MIN_CHANGE_PCT", "SURPRISE_SHOCKER_MIN_RVOL", "SURPRISE_ORB_ATR_FRACTION",
    "SURPRISE_ORB_FALLBACK_PCT", "SURPRISE_FEED_OPEN_TTL_SEC", "SURPRISE_FEED_COOLDOWN_SEC",
    "SURPRISE_FEED_MIN_FORCE_INTERVAL_SEC", "CACHE_DATABASE_URL", "DATABASE_URL", "TRAINING_DATABASE_URL",
    "ORACLE_DSN", "MARKET_DATA_URL",
)


# ── fakes ─────────────────────────────────────────────────────────────────────

def run(coro):
    return asyncio.run(coro)


def boom(*a, **k):
    raise RuntimeError("boom")


class Clock:
    """Fake `time` module. time() advances by `step` per call; sleep() advances and records."""

    def __init__(self, now=1_000_000.0, step=0.0):
        self.now = now
        self.step = step
        self.sleeps = []

    def time(self):
        self.now += self.step
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


class Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class Loader:
    def __init__(self, monkeypatch):
        self.mp = monkeypatch
        self.n = 0

    def __call__(self, **env):
        for k in _ENV_KEYS:
            self.mp.delenv(k, raising=False)
        for k, v in env.items():
            self.mp.setenv(k, v)
        self.n += 1
        spec = importlib.util.spec_from_file_location(f"surprise_scanner_under_test_{self.n}", _MOD_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


@pytest.fixture
def load(monkeypatch):
    return Loader(monkeypatch)


@pytest.fixture
def sc(load):
    return load()


@pytest.fixture
def eng(sc):
    return sc.SurpriseStockEngine()


def schema_fake(**fns):
    m = types.ModuleType("surprise_schema")
    for k, v in fns.items():
        setattr(m, k, v)
    return m


class FakeKV(types.ModuleType):
    """Fake kv_cache: get/set back the durable last-result cache; kv_get/kv_set back the feed cache."""

    def __init__(self):
        super().__init__("kv_cache")
        self.store = {}
        self.stale = {}           # expired rows: visible to get_stale only (group131)
        self.get_stale_raises = None
        self.sets = []            # (key, value, ttl) for kv_cache.set
        self.kv_sets = []         # (key, value, ttl) for kv_cache.kv_set
        self.get_raises = None
        self.set_raises = None
        self.kv_get_raises = None
        self.kv_set_raises = None

    def get(self, key):
        if self.get_raises is not None:
            raise self.get_raises
        return self.store.get(key)

    def get_stale(self, key):
        if self.get_stale_raises is not None:
            raise self.get_stale_raises
        return self.store.get(key) if self.store.get(key) is not None else self.stale.get(key)

    def set(self, key, value, ttl=None):
        if self.set_raises is not None:
            raise self.set_raises
        self.sets.append((key, value, ttl))
        self.store[key] = value

    def kv_get(self, key):
        if self.kv_get_raises is not None:
            raise self.kv_get_raises
        return self.store.get(key)

    def kv_set(self, key, value, ttl=None):
        if self.kv_set_raises is not None:
            raise self.kv_set_raises
        self.kv_sets.append((key, value, ttl))
        self.store[key] = value


@pytest.fixture
def kv(monkeypatch):
    fake = FakeKV()
    monkeypatch.setitem(sys.modules, "kv_cache", fake)
    return fake


class Res:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, db):
        self.db = db
        self.dialect = SimpleNamespace(name=db.dialect_name)

    def execute(self, stmt, params=None):
        self.db.sqls.append(str(stmt))
        return Res(self.db.rows)


class _Ctx:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return FakeConn(self.db)

    def __exit__(self, *a):
        return False


class FakeDB:
    """Fake engine: begin()/connect() yield a conn recording every SQL statement."""

    def __init__(self, rows=(), dialect_name="postgresql"):
        self.rows = list(rows)
        self.dialect_name = dialect_name
        self.sqls = []
        self.begin_raises = None
        self.connect_raises = None

    def begin(self):
        if self.begin_raises is not None:
            raise self.begin_raises
        return _Ctx(self)

    def connect(self):
        if self.connect_raises is not None:
            raise self.connect_raises
        return _Ctx(self)


def static_row(**over):
    r = {"prev_close": 100.0, "avg_15m_volume": 10000, "daily_atr": 2.0, "high_52w": 110.0,
         "dist_52w_pct": 5.0, "sector": "IT", "is_liquid": True}
    r.update(over)
    return r


def install_clock(monkeypatch, y=2026, mo=9, d=30, h=11, mi=0):
    """Fake `zoneinfo` + `datetime` so 'now in IST' is exactly the given wall-clock time."""
    zmod = types.ModuleType("zoneinfo")
    zmod.ZoneInfo = lambda name: _real_dt.timezone(_real_dt.timedelta(hours=5, minutes=30))
    dmod = types.ModuleType("datetime")
    dmod.__dict__.update({k: v for k, v in vars(_real_dt).items() if not k.startswith("__")})

    class FakeDT(_real_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _real_dt.datetime(y, mo, d, h, mi, tzinfo=tz)

    dmod.datetime = FakeDT
    monkeypatch.setitem(sys.modules, "zoneinfo", zmod)
    monkeypatch.setitem(sys.modules, "datetime", dmod)


# ── module constants / env ────────────────────────────────────────────────────

class TestConstants:
    def test_defaults(self, sc):
        assert (sc.MIN_SCORE, sc.MIN_CHANGE_PCT, sc.HARD_FLOOR_LIQUIDITY) == (65, 1.5, 5_000_000.0)
        assert (sc.CONCURRENCY, sc.QUOTE_TIMEOUT, sc.SURPRISE_CACHE_MAX_AGE_SEC) == (20, 3.0, 220.0)
        assert (sc.MAX_STOCK_PRICE, sc.VALUE_BUY_THRESHOLD) == (0.0, 500.0)
        assert (sc.BUILDING_MIN_SCORE, sc.BUILDING_MAX_SCORE, sc.BUILDING_MIN_CHANGE_PCT) == (35, 65, 0.3)
        assert (sc.RVOL_SLOPE_MIN, sc.SECTOR_SYMPATHY_MIN_SCORE) == (0.6, 45)
        assert (sc.DIST_52W_BREAKOUT_PCT, sc.DIST_52W_NEAR_PCT) == (8.0, 15.0)
        assert (sc.SHOCKER_MIN_CHANGE_PCT, sc.SHOCKER_MIN_RVOL) == (5.0, 2.0)
        assert (sc.ORB_ATR_FRACTION, sc.ORB_FALLBACK_PCT) == (0.3, 0.005)
        assert sc.SURPRISE_LAST_RESULT_CACHE_KEY == "stockky:surprise_scan:last_result"
        assert sc.SURPRISE_FEED_CACHE_KEY == "system:surprise_feed"
        assert (sc.SURPRISE_FEED_OPEN_TTL_SEC, sc.SURPRISE_FEED_COOLDOWN_SEC,
                sc.SURPRISE_FEED_MIN_FORCE_INTERVAL_SEC) == (7200, 0.5, 300.0)

    def test_env_overrides(self, load):
        m = load(SURPRISE_MIN_SCORE="50", SURPRISE_MIN_CHANGE_PCT="2.5", HARD_FLOOR_LIQUIDITY="1000",
                 SURPRISE_SCAN_CONCURRENCY="8", SURPRISE_QUOTE_TIMEOUT="9", SURPRISE_CACHE_MAX_AGE_SEC="60",
                 MAX_STOCK_PRICE="750", VALUE_BUY_THRESHOLD="300", SURPRISE_BUILDING_MIN_SCORE="30",
                 SURPRISE_BUILDING_MIN_CHANGE_PCT="0.1", SURPRISE_RVOL_SLOPE_MIN="0.9",
                 SURPRISE_SECTOR_SYMPATHY_MIN_SCORE="20", SURPRISE_DIST_52W_BREAKOUT_PCT="4",
                 SURPRISE_DIST_52W_NEAR_PCT="9", SURPRISE_SHOCKER_MIN_CHANGE_PCT="7",
                 SURPRISE_SHOCKER_MIN_RVOL="3", SURPRISE_ORB_ATR_FRACTION="0.5",
                 SURPRISE_ORB_FALLBACK_PCT="0.01", SURPRISE_FEED_OPEN_TTL_SEC="10",
                 SURPRISE_FEED_COOLDOWN_SEC="0.1", SURPRISE_FEED_MIN_FORCE_INTERVAL_SEC="20")
        assert (m.MIN_SCORE, m.MIN_CHANGE_PCT, m.HARD_FLOOR_LIQUIDITY) == (50, 2.5, 1000.0)
        assert (m.CONCURRENCY, m.QUOTE_TIMEOUT, m.SURPRISE_CACHE_MAX_AGE_SEC) == (8, 9.0, 60.0)
        assert (m.MAX_STOCK_PRICE, m.VALUE_BUY_THRESHOLD) == (750.0, 300.0)
        assert (m.BUILDING_MIN_SCORE, m.BUILDING_MAX_SCORE, m.BUILDING_MIN_CHANGE_PCT) == (30, 50, 0.1)
        assert (m.RVOL_SLOPE_MIN, m.SECTOR_SYMPATHY_MIN_SCORE) == (0.9, 20)
        assert (m.DIST_52W_BREAKOUT_PCT, m.DIST_52W_NEAR_PCT) == (4.0, 9.0)
        assert (m.SHOCKER_MIN_CHANGE_PCT, m.SHOCKER_MIN_RVOL) == (7.0, 3.0)
        assert (m.ORB_ATR_FRACTION, m.ORB_FALLBACK_PCT) == (0.5, 0.01)
        assert (m.SURPRISE_FEED_OPEN_TTL_SEC, m.SURPRISE_FEED_COOLDOWN_SEC,
                m.SURPRISE_FEED_MIN_FORCE_INTERVAL_SEC) == (10, 0.1, 20.0)

    def test_blank_price_caps_fall_back_to_their_defaults(self, load):
        m = load(MAX_STOCK_PRICE="", VALUE_BUY_THRESHOLD="")
        assert (m.MAX_STOCK_PRICE, m.VALUE_BUY_THRESHOLD) == (0.0, 500.0)

    def test_module_singleton_engine(self, sc):
        assert isinstance(sc.surprise_engine, sc.SurpriseStockEngine)


class TestDerivativeContractRegex:
    @pytest.mark.parametrize("sym", [
        "APLAPOLLO29SEP26FUT", "BANKNIFTY29SEP2648000CE", "NIFTY30OCT2624500PE", "RELIANCE26DEC26FUT",
        "X01JAN262500.5CE",
    ])
    def test_contracts_match(self, sc, sym):
        assert sc._DERIVATIVE_CONTRACT_RE.search(sym)

    @pytest.mark.parametrize("sym", ["TCS", "M&M", "SEP26", "29SEP26", "BAJAJ-AUTO", "FUT", "20FUTURE"])
    def test_equities_do_not_match(self, sc, sym):
        assert not sc._DERIVATIVE_CONTRACT_RE.search(sym)


# ── _normalize_db_url ─────────────────────────────────────────────────────────

class TestNormalizeDbUrl:
    @pytest.mark.parametrize("url,want", [
        ("postgres://u:p@h/db", "postgresql://u:p@h/db?sslmode=require"),
        ("postgresql://h/db", "postgresql://h/db?sslmode=require"),
        ("postgresql://h/db?sslmode=required", "postgresql://h/db?sslmode=require"),
        ("postgresql://h/db?sslmode=disable", "postgresql://h/db?sslmode=disable"),
        ("postgresql://h/db?channel_binding=require", "postgresql://h/db?sslmode=require"),
        ("postgresql://h/db?channel_binding=require&sslmode=require", "postgresql://h/db?sslmode=require"),
        ("postgresql://h/db?sslmode=require&channel_binding=require", "postgresql://h/db?sslmode=require"),
        ("postgresql://h/db?a=1&channel_binding=require&b=2", "postgresql://h/db?a=1&b=2&sslmode=require"),
        ("postgresql://h/db?a=1", "postgresql://h/db?a=1&sslmode=require"),
    ])
    def test_cases(self, sc, url, want):
        assert sc._normalize_db_url(url) == want


# ── dedupe_by_symbol / directional_filter ─────────────────────────────────────

class TestDedupeBySymbol:
    def test_empty(self, sc):
        assert sc.dedupe_by_symbol([]) == []

    def test_rows_without_a_symbol_are_dropped(self, sc):
        assert sc.dedupe_by_symbol([{"score": 1}, {"symbol": "", "score": 2}, {"symbol": None}]) == []

    def test_unique_symbols_keep_first_seen_order(self, sc):
        rows = [{"symbol": "B"}, {"symbol": "A"}]
        assert sc.dedupe_by_symbol(rows) == rows

    def test_higher_score_wins(self, sc):
        out = sc.dedupe_by_symbol([{"symbol": "A", "score": 50}, {"symbol": "A", "score": 70}])
        assert [r["score"] for r in out] == [70]
        out = sc.dedupe_by_symbol([{"symbol": "A", "score": 70}, {"symbol": "A", "score": 50}])
        assert [r["score"] for r in out] == [70]

    def test_missing_score_counts_as_zero(self, sc):
        out = sc.dedupe_by_symbol([{"symbol": "A"}, {"symbol": "A", "score": 1}])
        assert out[0]["score"] == 1

    def test_equal_score_prefers_the_newer_updated_at(self, sc):
        out = sc.dedupe_by_symbol([{"symbol": "A", "score": 5, "updated_at": "2026-09-29"},
                                   {"symbol": "A", "score": 5, "updated_at": "2026-09-30"}])
        assert out[0]["updated_at"] == "2026-09-30"
        out = sc.dedupe_by_symbol([{"symbol": "A", "score": 5, "updated_at": "2026-09-30"},
                                   {"symbol": "A", "score": 5, "updated_at": "2026-09-29"}])
        assert out[0]["updated_at"] == "2026-09-30"

    def test_equal_score_and_unstringable_timestamp_keeps_the_first(self, sc):
        class Bad:
            def __str__(self):
                raise ValueError("no str")

        first = {"symbol": "A", "score": 5, "updated_at": "x"}
        out = sc.dedupe_by_symbol([first, {"symbol": "A", "score": 5, "updated_at": Bad()}])
        assert out == [first]


class TestDirectionalFilter:
    def test_splits_on_sign(self, sc):
        pos, neg = sc.directional_filter([{"change_pct": 1.0}, {"pct_change": -1.0}, {"change_pct": 0}, {}])
        assert pos == [{"change_pct": 1.0}]
        assert neg == [{"pct_change": -1.0}, {"change_pct": 0}, {}]

    def test_pct_change_takes_precedence_over_change_pct(self, sc):
        row = {"pct_change": 2.0, "change_pct": -5.0}
        assert sc.directional_filter([row]) == ([row], [])

    def test_empty(self, sc):
        assert sc.directional_filter([]) == ([], [])


# ── _session_progress_ist ─────────────────────────────────────────────────────

class TestSessionProgress:
    @pytest.mark.parametrize("h,mi,want", [
        (8, 0, 0.15), (9, 15, 0.15), (9, 20, 0.15), (12, 22, 187 / 375), (15, 29, 374 / 375),
        (15, 30, 1.0), (18, 0, 1.0),
    ])
    def test_fraction_of_the_session(self, sc, monkeypatch, h, mi, want):
        install_clock(monkeypatch, h=h, mi=mi)
        assert sc._session_progress_ist() == pytest.approx(want)

    def test_clock_failure_falls_back_to_the_old_constant(self, sc, monkeypatch):
        monkeypatch.setitem(sys.modules, "zoneinfo", None)
        assert sc._session_progress_ist() == 0.4


# ── schema / dialect / url / engine plumbing ──────────────────────────────────

class TestDialect:
    def test_from_schema(self, sc, monkeypatch):
        monkeypatch.setattr(sc, "_ss", schema_fake(dialect=lambda: "oracle"))
        assert sc._dialect() == "oracle"

    def test_schema_error_falls_back_to_env(self, sc, monkeypatch):
        monkeypatch.setattr(sc, "_ss", schema_fake(dialect=boom))
        assert sc._dialect() == "postgresql"
        monkeypatch.setenv("ORACLE_DSN", "dsn")
        assert sc._dialect() == "oracle"

    def test_no_schema_uses_env(self, sc, monkeypatch):
        monkeypatch.setattr(sc, "_ss", None)
        assert sc._dialect() == "postgresql"
        monkeypatch.setenv("ORACLE_DSN", "dsn")
        assert sc._dialect() == "oracle"


class TestConnDialect:
    def test_reads_the_connection_dialect_lowercased(self, sc):
        assert sc._conn_dialect(SimpleNamespace(dialect=SimpleNamespace(name="ORACLE"))) == "oracle"

    def test_empty_name_gives_empty_string(self, sc):
        assert sc._conn_dialect(SimpleNamespace(dialect=SimpleNamespace(name=None))) == ""

    def test_broken_connection_falls_back_to_dialect(self, sc, monkeypatch):
        monkeypatch.setattr(sc, "_ss", None)
        assert sc._conn_dialect(object()) == "postgresql"


class TestImportGuard:
    def test_missing_schema_module_sets_none(self, load, monkeypatch):
        monkeypatch.setitem(sys.modules, "surprise_schema", None)
        assert load()._ss is None

    def test_schema_module_is_used_when_present(self, load, monkeypatch):
        fake = schema_fake()
        monkeypatch.setitem(sys.modules, "surprise_schema", fake)
        assert load()._ss is fake


class TestDbUrl:
    def test_prefers_the_schema_url(self, sc, monkeypatch):
        monkeypatch.setattr(sc, "_ss", schema_fake(database_url=lambda: "postgresql://s/db"))
        assert sc._db_url() == "postgresql://s/db"

    def test_schema_error_falls_back_to_env(self, sc, monkeypatch, caplog):
        monkeypatch.setattr(sc, "_ss", schema_fake(database_url=boom))
        monkeypatch.setenv("DATABASE_URL", "postgres://h/db")
        with caplog.at_level(logging.DEBUG):
            assert sc._db_url() == "postgresql://h/db?sslmode=require"
        assert "surprise_schema.database_url" in caplog.text

    def test_env_precedence(self, sc, monkeypatch):
        monkeypatch.setattr(sc, "_ss", None)
        for k, v in (("CACHE_DATABASE_URL", "postgresql://c/x"), ("DATABASE_URL", "postgresql://d/x"),
                     ("TRAINING_DATABASE_URL", "postgresql://t/x")):
            monkeypatch.setenv(k, v)
        assert sc._db_url().startswith("postgresql://c/x")
        monkeypatch.delenv("CACHE_DATABASE_URL")
        assert sc._db_url().startswith("postgresql://d/x")
        monkeypatch.delenv("DATABASE_URL")
        assert sc._db_url().startswith("postgresql://t/x")

    def test_none_when_unset(self, sc, monkeypatch):
        monkeypatch.setattr(sc, "_ss", None)
        assert sc._db_url() is None

    def test_oracle_url_is_none(self, sc, monkeypatch):
        monkeypatch.setattr(sc, "_ss", None)
        monkeypatch.setenv("DATABASE_URL", "ORACLE+oracledb://u:p@dsn")
        assert sc._db_url() is None


class TestEngine:
    def test_uses_the_shared_engine(self, sc, monkeypatch):
        seen = []
        monkeypatch.setattr(sc, "_ss", schema_fake(shared_engine=lambda app: seen.append(app) or "SHARED"))
        assert sc._engine("app-x") == "SHARED"
        assert seen == ["app-x"]

    def test_make_engine_when_there_is_no_shared_engine(self, sc, monkeypatch):
        monkeypatch.setattr(sc, "_ss", schema_fake(make_engine=lambda app: ("MADE", app)))
        assert sc._engine("app-y") == ("MADE", "app-y")

    def test_schema_error_falls_back_to_create_engine(self, sc, monkeypatch):
        import sqlalchemy
        monkeypatch.setattr(sqlalchemy, "create_engine", lambda url, **kw: SimpleNamespace(url=url, kw=kw))
        monkeypatch.setattr(sc, "_ss", schema_fake(shared_engine=boom, database_url=lambda: "postgresql://h/db"))
        e = sc._engine("app-z")
        assert e.url == "postgresql://h/db"
        assert e.kw["pool_pre_ping"] is True
        assert (e.kw["pool_size"], e.kw["max_overflow"], e.kw["pool_timeout"]) == (1, 1, 8)
        assert e.kw["connect_args"] == {"connect_timeout": 8, "application_name": "app-z"}

    def test_no_url_gives_none(self, sc, monkeypatch):
        monkeypatch.setattr(sc, "_ss", None)
        assert sc._engine("app") is None


# ── engine construction + durable last-result cache ───────────────────────────

class TestEngineInit:
    def test_initial_state(self, eng):
        assert eng.static_cache == {}
        assert eng._loaded_at == 0.0
        assert eng._last_rvol == {}
        assert eng._last_scan_ts == 0.0
        assert eng._last_result is None

    @pytest.mark.parametrize("conc,want", [("1", 4), ("3", 4), ("4", 4), ("10", 10), ("20", 20), ("99", 20)])
    def test_semaphore_is_clamped_to_4_20(self, load, conc, want):
        m = load(SURPRISE_SCAN_CONCURRENCY=conc)
        assert m.SurpriseStockEngine().semaphore._value == want


class TestLoadLastResultFromDurableCache:
    def test_restores_result_and_ts(self, eng, kv):
        kv.store["stockky:surprise_scan:last_result"] = {"result": {"count": 3}, "scan_ts": "123.5"}
        eng._load_last_result_from_durable_cache()
        assert eng._last_result == {"count": 3}
        assert eng._last_scan_ts == 123.5

    @pytest.mark.parametrize("payload", [
        None, "str", {"result": "notdict", "scan_ts": 1}, {"result": {"a": 1}}, {"result": {"a": 1}, "scan_ts": 0},
    ])
    def test_ignores_unusable_payload(self, eng, kv, payload):
        kv.store["stockky:surprise_scan:last_result"] = payload
        eng._load_last_result_from_durable_cache()
        assert eng._last_result is None
        assert eng._last_scan_ts == 0.0

    def test_unparseable_ts_is_swallowed(self, eng, kv):
        kv.store["stockky:surprise_scan:last_result"] = {"result": {"a": 1}, "scan_ts": "abc"}
        eng._load_last_result_from_durable_cache()
        assert eng._last_result == {"a": 1}  # assigned before the float() blew up
        assert eng._last_scan_ts == 0.0

    def test_kv_read_error_is_swallowed(self, eng, kv):
        kv.get_raises = RuntimeError("neon down")
        eng._load_last_result_from_durable_cache()
        assert eng._last_result is None

    def test_missing_kv_module_is_swallowed(self, eng, monkeypatch):
        monkeypatch.setitem(sys.modules, "kv_cache", None)
        eng._load_last_result_from_durable_cache()
        assert eng._last_result is None


class TestLoadLastResultStaleFromDurableCache:
    """group131: the saved last result expires ~340 s after the scan, so overnight only get_stale sees it."""

    KEY = "stockky:surprise_scan:last_result"

    def test_plain_read_misses_but_stale_read_restores(self, eng, kv):
        kv.stale[self.KEY] = {"result": {"count": 7}, "scan_ts": "100.0"}
        eng._load_last_result_from_durable_cache()
        assert eng._last_result is None
        eng._load_last_result_stale_from_durable_cache()
        assert eng._last_result == {"count": 7} and eng._last_scan_ts == 100.0

    @pytest.mark.parametrize("payload", [None, "str", {"result": "x", "scan_ts": 1}, {"result": {"a": 1}}])
    def test_unusable_payload_is_ignored(self, eng, kv, payload):
        kv.stale[self.KEY] = payload
        eng._load_last_result_stale_from_durable_cache()
        assert eng._last_result is None and eng._last_scan_ts == 0.0

    def test_errors_are_swallowed(self, eng, kv, monkeypatch):
        kv.get_stale_raises = RuntimeError("db down")
        eng._load_last_result_stale_from_durable_cache()
        assert eng._last_result is None
        monkeypatch.setitem(sys.modules, "kv_cache", None)
        eng._load_last_result_stale_from_durable_cache()
        assert eng._last_result is None

    def test_a_stale_restore_is_not_served_as_fresh(self, eng, kv):
        import time as _t
        kv.stale[self.KEY] = {"result": {"count": 7}, "scan_ts": str(_t.time() - 6 * 3600)}
        eng._load_last_result_stale_from_durable_cache()
        assert eng._last_result is not None
        assert (_t.time() - eng._last_scan_ts) > 220  # scan(cached=True) age check rejects it


# ── load_static_cache ─────────────────────────────────────────────────────────

@pytest.fixture
def db_setup(sc, monkeypatch):
    """Engine wired to a FakeDB. Returns (engine, db, clock)."""
    clock = Clock()
    monkeypatch.setattr(sc, "time", clock)
    monkeypatch.setitem(sys.modules, "surprise_schema", schema_fake(ensure_surprise_schema=lambda: {"ok": True}))
    db = FakeDB()
    monkeypatch.setattr(sc, "_db_url", lambda: "postgresql://h/db")
    monkeypatch.setattr(sc, "_engine", lambda app: db)
    e = sc.SurpriseStockEngine()
    monkeypatch.setattr(e, "_seed_from_data_feed_kv", lambda cache: 0)
    return e, db, clock


def db_row(symbol="TCS", **over):
    r = {"symbol": symbol, "prev_close": 100, "avg_15m_volume": 500, "daily_atr": 2, "high_52w": 120,
         "dist_52w_pct": 5, "sector": "IT", "is_liquid": True, "updated_at": None}
    r.update(over)
    return r


class TestLoadStaticCache:
    def test_loads_and_normalises_rows(self, db_setup):
        e, db, clock = db_setup
        db.rows = [db_row(" tcs ", prev_close="101.5", daily_atr=None, high_52w="x",
                          dist_52w_pct=decimal.Decimal(7))]
        assert e.load_static_cache() == 1
        d = e.static_cache["TCS"]
        assert d["prev_close"] == 101.5
        assert d["daily_atr"] == 0.0        # None -> 0.0
        assert d["high_52w"] == 0.0         # unparseable -> 0.0
        assert d["dist_52w_pct"] == 7.0
        assert d["avg_15m_volume"] == 500
        assert e._loaded_at == clock.now

    def test_creates_table_on_postgres_only(self, db_setup):
        e, db, _ = db_setup
        e.load_static_cache()
        assert any("CREATE TABLE IF NOT EXISTS surprise_static_feed" in s for s in db.sqls)
        assert any(s.startswith("SELECT symbol, prev_close") for s in db.sqls)

    def test_oracle_skips_create_table(self, db_setup):
        e, db, _ = db_setup
        db.dialect_name = "oracle"
        e.load_static_cache()
        assert not any("CREATE TABLE" in s for s in db.sqls)
        assert any(s.startswith("SELECT symbol, prev_close") for s in db.sqls)

    def test_fresh_cache_short_circuits(self, db_setup):
        e, db, clock = db_setup
        e.static_cache = {"A": {}, "B": {}}
        e._loaded_at = clock.now - 299
        assert e.load_static_cache() == 2
        assert db.sqls == []

    def test_stale_cache_reloads(self, db_setup):
        e, db, clock = db_setup
        e.static_cache = {"A": {}}
        e._loaded_at = clock.now - 300
        db.rows = [db_row("TCS")]
        assert e.load_static_cache() == 1
        assert "TCS" in e.static_cache and "A" not in e.static_cache

    def test_force_reload_ignores_freshness(self, db_setup):
        e, db, clock = db_setup
        e.static_cache = {"A": {}}
        e._loaded_at = clock.now
        db.rows = [db_row("TCS")]
        assert e.load_static_cache(force=True) == 1
        assert list(e.static_cache) == ["TCS"]

    def test_no_url_empties_cache(self, sc, monkeypatch, caplog):
        monkeypatch.setattr(sc, "_db_url", lambda: None)
        e = sc.SurpriseStockEngine()
        e.static_cache = {"A": {}}
        e._loaded_at = 0.0
        with caplog.at_level(logging.WARNING):
            assert e.load_static_cache() == 0
        assert e.static_cache == {}
        assert "no DATABASE_URL" in caplog.text

    def test_schema_ensure_failure_is_not_fatal(self, db_setup, monkeypatch):
        e, db, _ = db_setup
        monkeypatch.setitem(sys.modules, "surprise_schema", schema_fake(ensure_surprise_schema=boom))
        db.rows = [db_row("TCS")]
        assert e.load_static_cache() == 1

    def test_missing_schema_module_is_not_fatal(self, db_setup, monkeypatch):
        e, db, _ = db_setup
        monkeypatch.setitem(sys.modules, "surprise_schema", None)
        db.rows = [db_row("TCS")]
        assert e.load_static_cache() == 1

    def test_engine_none_gives_empty(self, sc, monkeypatch):
        monkeypatch.setitem(sys.modules, "surprise_schema", None)
        monkeypatch.setattr(sc, "_db_url", lambda: "postgresql://h/db")
        monkeypatch.setattr(sc, "_engine", lambda app: None)
        e = sc.SurpriseStockEngine()
        e.static_cache = {"A": {}}
        assert e.load_static_cache() == 0
        assert e.static_cache == {}

    def test_blank_symbol_rows_are_skipped(self, db_setup):
        e, db, _ = db_setup
        db.rows = [db_row(""), db_row(None), db_row("   "), db_row("INFY")]
        assert e.load_static_cache() == 1
        assert list(e.static_cache) == ["INFY"]

    @pytest.mark.parametrize("raw,want", [
        (True, True), (False, False), (1, True), (0, False), ("1", True), ("0", False),
        ("garbage", True), (None, True),
    ])
    def test_is_liquid_coercion(self, db_setup, raw, want):
        e, db, _ = db_setup
        db.rows = [db_row("TCS", is_liquid=raw)]
        e.load_static_cache()
        assert e.static_cache["TCS"]["is_liquid"] is want

    def test_missing_is_liquid_key_is_left_alone(self, db_setup):
        e, db, _ = db_setup
        r = db_row("TCS")
        del r["is_liquid"]
        db.rows = [r]
        e.load_static_cache()
        assert "is_liquid" not in e.static_cache["TCS"]

    @pytest.mark.parametrize("over,want", [
        ({"avg_15m_volume": 750}, 750),
        ({"avg_15m_volume": 0, "avg_volume": 900}, 900),
        ({"avg_15m_volume": None}, 10000),
        ({"avg_15m_volume": "12"}, 12),
        ({"avg_15m_volume": "abc"}, 10000),
        ({"avg_15m_volume": 0}, 10000),
    ])
    def test_avg_15m_volume_variants(self, db_setup, over, want):
        e, db, _ = db_setup
        db.rows = [db_row("TCS", **over)]
        e.load_static_cache()
        assert e.static_cache["TCS"]["avg_15m_volume"] == want

    def test_small_universe_triggers_kv_seed(self, db_setup, monkeypatch):
        e, db, _ = db_setup
        calls = []

        def seed(cache):
            calls.append(len(cache))
            cache["SEEDED"] = {"symbol": "SEEDED"}
            return 1

        monkeypatch.setattr(e, "_seed_from_data_feed_kv", seed)
        db.rows = [db_row("TCS")]
        assert e.load_static_cache() == 2
        assert calls == [1]
        assert "SEEDED" in e.static_cache

    def test_small_universe_but_nothing_seeded(self, db_setup):
        e, db, _ = db_setup
        db.rows = [db_row("TCS")]
        assert e.load_static_cache() == 1

    def test_large_universe_skips_seed(self, db_setup, monkeypatch):
        e, db, _ = db_setup
        monkeypatch.setattr(e, "_seed_from_data_feed_kv", lambda c: pytest.fail("seed must not run"))
        db.rows = [db_row(f"S{i}") for i in range(20)]
        assert e.load_static_cache() == 20

    def test_query_failure_falls_back_to_seed(self, db_setup, monkeypatch, caplog):
        e, db, clock = db_setup
        db.begin_raises = RuntimeError("db down")
        e.static_cache = {"OLD": {"symbol": "OLD"}}

        def seed(cache):
            cache["NEW"] = {}
            return 1

        monkeypatch.setattr(e, "_seed_from_data_feed_kv", seed)
        with caplog.at_level(logging.WARNING):
            assert e.load_static_cache() == 2
        assert set(e.static_cache) == {"OLD", "NEW"}
        assert e._loaded_at == clock.now
        assert "load_static_cache failed" in caplog.text

    def test_query_failure_with_nothing_seeded_keeps_old_cache(self, db_setup):
        e, db, _ = db_setup
        db.begin_raises = RuntimeError("db down")
        e.static_cache = {"OLD": {}}
        assert e.load_static_cache() == 1
        assert e._loaded_at == 0.0

    def test_query_failure_and_seed_failure(self, db_setup, monkeypatch):
        e, db, _ = db_setup
        db.begin_raises = RuntimeError("db down")

        def bad_seed(cache):
            raise RuntimeError("seed boom")

        monkeypatch.setattr(e, "_seed_from_data_feed_kv", bad_seed)
        assert e.load_static_cache() == 0


# ── _seed_from_data_feed_kv ───────────────────────────────────────────────────

@pytest.fixture
def seeder(sc, monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(sc, "_db_url", lambda: "postgresql://h/db")
    monkeypatch.setattr(sc, "_engine", lambda app: db)
    return sc, db, sc.SurpriseStockEngine()


class TestSeedFromDataFeedKv:
    def test_no_url(self, sc, monkeypatch):
        monkeypatch.setattr(sc, "_db_url", lambda: None)
        assert sc.SurpriseStockEngine()._seed_from_data_feed_kv({}) == 0

    def test_engine_none(self, sc, monkeypatch):
        monkeypatch.setattr(sc, "_db_url", lambda: "postgresql://h/db")
        monkeypatch.setattr(sc, "_engine", lambda app: None)
        assert sc.SurpriseStockEngine()._seed_from_data_feed_kv({}) == 0

    def test_sqlalchemy_missing(self, sc, monkeypatch):
        monkeypatch.setattr(sc, "_db_url", lambda: "postgresql://h/db")
        monkeypatch.setitem(sys.modules, "sqlalchemy", None)
        assert sc.SurpriseStockEngine()._seed_from_data_feed_kv({}) == 0

    def test_connect_failure(self, seeder):
        sc, db, e = seeder
        db.connect_raises = RuntimeError("no conn")
        assert e._seed_from_data_feed_kv({}) == 0

    def test_limit_clause_per_dialect(self, seeder):
        sc, db, e = seeder
        e._seed_from_data_feed_kv({})
        assert "LIMIT 800" in db.sqls[-1] and "FETCH FIRST" not in db.sqls[-1]
        db.dialect_name = "oracle"
        e._seed_from_data_feed_kv({})
        assert "FETCH FIRST 800 ROWS ONLY" in db.sqls[-1]

    def test_builds_baseline_from_payload(self, seeder):
        sc, db, e = seeder
        db.rows = [("stockky:data_feed:infy.ns",
                    {"payload": {"price": 1500, "previous_close": 1450, "volume": 400000,
                                 "day_high": 1510, "sector": "IT"}})]
        cache = {}
        assert e._seed_from_data_feed_kv(cache) == 1
        assert cache["INFY"] == {
            "symbol": "INFY", "prev_close": 1450.0, "avg_15m_volume": 20000, "daily_atr": 50.0,
            "high_52w": 1510.0, "dist_52w_pct": 5.0, "sector": "IT", "is_liquid": True,
            "source": "data_feed_kv",
        }

    def test_flat_payload_and_defaults(self, seeder):
        sc, db, e = seeder
        db.rows = [("feed:abc.bo", {"cmp": 200})]
        cache = {}
        e._seed_from_data_feed_kv(cache)
        row = cache["ABC"]
        assert row["prev_close"] == 200.0          # previous_close missing -> price
        assert row["avg_15m_volume"] == 1000       # 10000 // 20 = 500 -> floored to 1000
        assert row["daily_atr"] == pytest.approx(3.0)  # |price-prev| == 0 -> 1.5% of price
        assert row["high_52w"] == 200.0
        assert row["sector"] == ""

    @pytest.mark.parametrize("key,sym", [
        ("stockky:data_feed:AAA", "AAA"), ("feed:BBB", "BBB"), ("data_feed:CCC", "CCC"),
        ("DDD.NS", "DDD"), ("eee.bo", "EEE"), ("  FFF  ", "FFF"),
    ])
    def test_key_prefix_and_suffix_stripping(self, seeder, key, sym):
        sc, db, e = seeder
        db.rows = [(key, {"price": 10})]
        cache = {}
        e._seed_from_data_feed_kv(cache)
        assert list(cache) == [sym]

    def test_json_string_values_are_parsed(self, seeder):
        sc, db, e = seeder
        db.rows = [("feed:AAA", json.dumps({"price": 50}))]
        assert e._seed_from_data_feed_kv({}) == 1

    @pytest.mark.parametrize("raw", ["{not json", 42, None, "[1, 2]", "null"])
    def test_unusable_values_are_skipped(self, seeder, raw):
        sc, db, e = seeder
        db.rows = [("feed:AAA", raw)]
        cache = {}
        assert e._seed_from_data_feed_kv(cache) == 0
        assert cache == {}

    def test_skips_empty_known_and_derivative_symbols(self, seeder):
        sc, db, e = seeder
        good = {"price": 10}
        db.rows = [(None, good), ("feed:", good), ("feed:HAVE", good), ("feed:APLAPOLLO29SEP26FUT", good),
                   ("feed:BANKNIFTY29SEP2648000CE", good), ("feed:NEW", good)]
        cache = {"HAVE": {"symbol": "HAVE"}}
        assert e._seed_from_data_feed_kv(cache) == 1
        assert set(cache) == {"HAVE", "NEW"}

    @pytest.mark.parametrize("payload", [{}, {"price": 0}, {"price": "abc"}, {"price": -5}, {"cmp": None}])
    def test_skips_bad_price(self, seeder, payload):
        sc, db, e = seeder
        db.rows = [("feed:AAA", payload)]
        assert e._seed_from_data_feed_kv({}) == 0

    def test_skips_price_above_max_stock_price(self, load, monkeypatch):
        m = load(MAX_STOCK_PRICE="100")
        db = FakeDB([("feed:CHEAP", {"price": 100}), ("feed:DEAR", {"price": 100.01})])
        monkeypatch.setattr(m, "_db_url", lambda: "postgresql://h/db")
        monkeypatch.setattr(m, "_engine", lambda app: db)
        cache = {}
        assert m.SurpriseStockEngine()._seed_from_data_feed_kv(cache) == 1
        assert list(cache) == ["CHEAP"]

    def test_bad_prev_and_volume_fall_back(self, seeder):
        sc, db, e = seeder
        db.rows = [("feed:AAA", {"price": 100, "previous_close": "abc", "volume": "abc"})]
        cache = {}
        e._seed_from_data_feed_kv(cache)
        assert cache["AAA"]["prev_close"] == 100.0
        assert cache["AAA"]["avg_15m_volume"] == 1000

    def test_volume_scaled_by_20_with_floor(self, seeder):
        sc, db, e = seeder
        db.rows = [("feed:AAA", {"price": 10, "volume": 2_000_000}), ("feed:BBB", {"price": 10, "volume": 5000})]
        cache = {}
        e._seed_from_data_feed_kv(cache)
        assert cache["AAA"]["avg_15m_volume"] == 100000
        assert cache["BBB"]["avg_15m_volume"] == 1000

    def test_unparseable_day_high_only_affects_its_own_row(self, seeder):
        # Fixed: day_high is now guarded per row like price/prev/volume. A bad value falls back to the
        # price (same default as a missing day_high) instead of aborting the whole seed with 0.
        sc, db, e = seeder
        db.rows = [("feed:GOOD", {"price": 10}), ("feed:BAD", {"price": 12, "day_high": "abc"}),
                   ("feed:LATER", {"price": 20, "day_high": 25})]
        cache = {}
        assert e._seed_from_data_feed_kv(cache) == 3
        assert cache["BAD"]["high_52w"] == 12.0
        assert cache["LATER"]["high_52w"] == 25.0 and "GOOD" in cache


# ── score_stock ───────────────────────────────────────────────────────────────

PERM_ENV = dict(SURPRISE_MIN_SCORE="0", SURPRISE_MIN_CHANGE_PCT="-1000", HARD_FLOOR_LIQUIDITY="0")


def scored(mod, tick, static=None, sym="TCS", call_sym=None, monkeypatch=None, progress=0.5):
    """Score one tick against a one-row static cache. Session progress is pinned (no wall clock)."""
    e = mod.SurpriseStockEngine()
    e.static_cache = {sym: static if static is not None else static_row()}
    if monkeypatch is not None:
        monkeypatch.setattr(mod, "_session_progress_ist", lambda: progress)
    return e.score_stock(call_sym or sym, tick)


@pytest.fixture
def perm(load, monkeypatch):
    """Module with every gate opened, so each test can read the raw score / trigger of one bucket."""
    m = load(**PERM_ENV)
    monkeypatch.setattr(m, "_session_progress_ist", lambda: 0.5)
    return m


def flat_static(**over):
    """Static row that earns no 52W / range points by itself."""
    base = dict(dist_52w_pct=50.0, daily_atr=0.0)
    base.update(over)
    return static_row(**base)


def flat_tick(**over):
    t = {"price": 100.0, "open": 100.0, "high": 100.0, "low": 100.0}
    t.update(over)
    return t


class TestScoreStockLookup:
    def test_unknown_symbol(self, sc):
        assert scored(sc, {"price": 100}, sym="TCS", call_sym="NOPE") is None

    @pytest.mark.parametrize("call", ["tcs", "TCS.NS", "tcs.bo", "TCS"])
    def test_symbol_normalisation(self, perm, call):
        assert scored(perm, flat_tick(), flat_static(), call_sym=call)["symbol"] == "TCS"

    @pytest.mark.parametrize("key", ["price", "last_price", "ltp", "close", "cmp"])
    def test_price_key_precedence_chain(self, perm, key):
        h = scored(perm, {key: 111.0, "open": 100, "high": 111, "low": 100}, flat_static())
        assert h["price"] == 111.0

    def test_unparseable_price_falls_back_to_baseline(self, perm):
        assert scored(perm, {"price": "abc"}, flat_static())["price"] == 100.0

    def test_none_tick_falls_back_to_baseline(self, perm):
        assert scored(perm, None, flat_static())["price"] == 100.0

    def test_empty_tick_uses_baseline_and_default_change(self, sc):
        assert scored(sc, {}, flat_static()) is None  # 0% change: no tier

    def test_no_price_and_no_baseline(self, perm):
        assert scored(perm, {}, flat_static(prev_close=0)) is None

    def test_zero_baseline_uses_price_as_prev_close(self, perm):
        h = scored(perm, flat_tick(price=50, open=50, high=50, low=50), flat_static(prev_close=0))
        assert h["prev_close"] == 50.0
        assert h["change_pct"] == 0.0


class TestScoreStockVolume:
    @pytest.mark.parametrize("vol,rvol,pts", [
        (0, 0.0, 0), (14900, 1.49, 0), (15000, 1.5, 10), (19900, 1.99, 10), (20000, 2.0, 20),
        (34900, 3.49, 20), (35000, 3.5, 35), (90000, 9.0, 35),
    ])
    def test_rvol_bands(self, perm, vol, rvol, pts):
        h = scored(perm, flat_tick(vol_15m=vol), flat_static())
        assert h["rvol"] == rvol
        assert h["score"] == pts

    def test_volume_slope_bonus_and_trigger(self, perm):
        e = perm.SurpriseStockEngine()
        e.static_cache = {"TCS": flat_static()}
        first = e.score_stock("TCS", flat_tick(vol_15m=10000))
        assert first["rvol_slope"] == 0.0 and first["score"] == 0
        second = e.score_stock("TCS", flat_tick(vol_15m=16000))
        assert second["rvol"] == 1.6
        assert second["rvol_slope"] == 0.6
        assert second["score"] == 20          # 10 (rvol >= 1.5) + 10 (slope >= 0.6)
        assert second["trigger_type"] == "Volume Accelerating"

    def test_slope_just_under_threshold(self, perm):
        e = perm.SurpriseStockEngine()
        e.static_cache = {"TCS": flat_static()}
        e.score_stock("TCS", flat_tick(vol_15m=10000))
        h = e.score_stock("TCS", flat_tick(vol_15m=15900))
        assert h["rvol_slope"] == 0.59
        assert h["score"] == 10

    def test_slope_state_is_per_normalised_symbol(self, perm):
        e = perm.SurpriseStockEngine()
        e.static_cache = {"TCS": flat_static()}
        e.score_stock("tcs.ns", flat_tick(vol_15m=10000))
        assert e._last_rvol == {"TCS": 1.0}

    def test_volume_scaled_by_session_progress(self, perm):
        h = scored(perm, flat_tick(volume=100000, session_progress=0.5), flat_static())
        assert h["rvol"] == 0.8          # 100000 / (25 * 0.5) / 10000

    def test_session_progress_defaults_to_the_clock(self, perm, monkeypatch):
        monkeypatch.setattr(perm, "_session_progress_ist", lambda: 1.0)
        assert scored(perm, flat_tick(volume=100000), flat_static())["rvol"] == 0.4

    def test_session_progress_clamped_low(self, perm):
        h = scored(perm, flat_tick(volume=100000, session_progress=0.01), flat_static())
        assert h["rvol"] == 2.67         # progress floored at 0.15

    def test_session_progress_clamped_high(self, perm):
        h = scored(perm, flat_tick(volume=100000, session_progress=5), flat_static())
        assert h["rvol"] == 0.4          # progress capped at 1.0

    def test_explicit_vol_15m_is_not_rescaled(self, perm):
        assert scored(perm, flat_tick(vol_15m=8000, volume=100000), flat_static())["rvol"] == 0.8

    def test_missing_static_volume_defaults_to_one_share(self, perm):
        h = scored(perm, flat_tick(vol_15m=50), flat_static(avg_15m_volume=0))
        assert h["rvol"] == 50.0
        assert h["avg_traded_value"] == 0.0


class TestScoreStockOrbVwap:
    def test_orb_breakout_with_proxies(self, perm):
        # vwap proxy = (100+106+100)/3 = 102 ; atr=0 -> orb line = 100 + 0.5% = 100.5
        h = scored(perm, {"price": 105, "open": 100, "high": 106, "low": 100}, flat_static())
        assert h["score"] == 25
        assert h["trigger_type"] == "Morning ORB Breakout"

    def test_orb_proxy_uses_atr_fraction(self, perm):
        # atr=10 -> orb buffer 3.0 -> orb_high 103. price 102.5 beats vwap but not the ORB line.
        h = scored(perm, {"price": 102.5, "open": 100, "high": 102.5, "low": 99}, flat_static(daily_atr=10.0))
        assert h["score"] == 13           # above VWAP, below the ATR-derived ORB line; range 3.5 < 8
        assert h["trigger_type"] == "Above VWAP"

    def test_above_vwap_only(self, perm):
        h = scored(perm, {"price": 100.4, "open": 100, "high": 100.4, "low": 99}, flat_static())
        assert h["score"] == 13
        assert h["trigger_type"] == "Above VWAP"

    def test_below_vwap_scores_nothing(self, perm):
        h = scored(perm, {"price": 99, "open": 100, "high": 100, "low": 99}, flat_static())
        assert h["score"] == 0
        assert h["trigger_type"] == "Consolidation"

    def test_explicit_vwap_and_orb_high_are_honoured(self, perm):
        assert scored(perm, flat_tick(price=100.5, vwap=99, orb_high=101), flat_static())["score"] == 13
        assert scored(perm, flat_tick(price=100.5, vwap=99, orb_high=100), flat_static())["score"] == 25

    def test_explicit_zero_vwap_counts_as_supplied(self, perm):
        assert scored(perm, flat_tick(vwap=0, orb_high=1000), flat_static())["score"] == 13

    def test_above_vwap_replaces_volume_accelerating(self, perm):
        # Only "Consolidation" / "Volume Accelerating" can be current when the ORB/VWAP bucket runs,
        # so the `trigger_type in (...)` guard is always true (its else-arc is unreachable).
        e = perm.SurpriseStockEngine()
        e.static_cache = {"TCS": flat_static()}
        e.score_stock("TCS", flat_tick(vol_15m=10000))
        h = e.score_stock("TCS", flat_tick(price=100.4, high=100.4, low=99, vol_15m=16000))
        assert h["score"] == 10 + 10 + 13
        assert h["trigger_type"] == "Above VWAP"

    def test_missing_open_high_low_use_baseline_and_price(self, perm):
        assert scored(perm, {"price": 100.0}, flat_static())["score"] == 0

    def test_trailing_stop_anchors_to_lower_of_vwap_and_price(self, perm):
        assert scored(perm, flat_tick(vwap=110), flat_static())["trailing_stop"] == 98.5   # price < vwap
        assert scored(perm, flat_tick(vwap=90), flat_static())["trailing_stop"] == 88.65  # vwap < price


class TestScoreStockOrderBook:
    @pytest.mark.parametrize("buy,pts", [(75, 15), (74.9, 8), (65, 8), (64.9, 4), (60, 4), (59.9, 0), (0, 0), (100, 15)])
    def test_bands(self, perm, buy, pts):
        h = scored(perm, flat_tick(buy_pct=buy), flat_static())
        assert h["score"] == pts
        assert h["buy_pct"] == float(buy)

    def test_strong_buy_side_sets_trigger_only_from_consolidation(self, perm):
        assert scored(perm, flat_tick(buy_pct=80), flat_static())["trigger_type"] == "Buy-Side Imbalance"
        e = perm.SurpriseStockEngine()
        e.static_cache = {"TCS": flat_static()}
        e.score_stock("TCS", flat_tick(vol_15m=10000))
        h = e.score_stock("TCS", flat_tick(vol_15m=16000, buy_pct=80))
        assert h["trigger_type"] == "Volume Accelerating"

    def test_derived_from_bid_ask_quantities(self, perm):
        h = scored(perm, flat_tick(total_bid_qty=300, total_ask_qty=100), flat_static())
        assert h["buy_pct"] == 75.0
        assert h["score"] == 15

    def test_no_depth_defaults_to_neutral_fifty(self, perm):
        assert scored(perm, flat_tick(total_bid_qty=0, total_ask_qty=0), flat_static())["buy_pct"] == 50.0

    def test_none_depth_values_default_to_neutral(self, perm):
        h = scored(perm, flat_tick(total_bid_qty=None, total_ask_qty=None), flat_static())
        assert h["buy_pct"] == 50.0

    @pytest.mark.parametrize("bad", ["abc", [1], {}])
    def test_unparseable_buy_pct_defaults(self, perm, bad):
        assert scored(perm, flat_tick(buy_pct=bad), flat_static())["buy_pct"] == 50.0

    def test_string_buy_pct_is_coerced(self, perm):
        assert scored(perm, flat_tick(buy_pct="80"), flat_static())["buy_pct"] == 80.0


class TestScoreStock52WeekAndRange:
    @pytest.mark.parametrize("dist,pts", [(0.5, 15), (8.0, 15), (8.01, 7), (15.0, 7), (15.01, 0), (None, 0), (50, 0)])
    def test_distance_bands(self, perm, dist, pts):
        assert scored(perm, flat_tick(), flat_static(dist_52w_pct=dist))["score"] == pts

    def test_near_52w_high_trigger_needs_a_running_score_of_50(self, perm):
        strong = scored(perm, flat_tick(vol_15m=35000), flat_static(dist_52w_pct=5.0))     # 35 + 15
        assert strong["score"] == 50
        assert strong["trigger_type"] == "Near 52W High"
        weak = scored(perm, flat_tick(vol_15m=20000), flat_static(dist_52w_pct=5.0))       # 20 + 15
        assert weak["score"] == 35
        assert weak["trigger_type"] == "Consolidation"

    def test_dist_is_reported_rounded(self, perm):
        assert scored(perm, flat_tick(), flat_static(dist_52w_pct=5.678))["dist_52w_pct"] == 5.68

    @pytest.mark.parametrize("low,pts", [(98.3, 10), (98.6, 0)])
    def test_range_expansion(self, perm, low, pts):
        h = scored(perm, {"price": 98.5, "open": 100, "high": 100, "low": low}, flat_static(daily_atr=2.0))
        assert h["score"] == pts
        assert h["trigger_type"] == ("Range Expansion" if pts else "Consolidation")

    def test_range_needs_a_known_atr(self, perm):
        h = scored(perm, {"price": 98.5, "open": 100, "high": 200, "low": 98.5}, flat_static(daily_atr=0.0))
        assert h["score"] == 0


class TestScoreStockTiers:
    def test_full_breakout_card(self, sc, monkeypatch):
        tick = {"price": 105, "open": 100, "high": 106, "low": 100, "vol_15m": 40000, "buy_pct": 80}
        h = scored(sc, tick, monkeypatch=monkeypatch)
        assert h["tier"] == "breakout"
        assert h["score"] == 100 and isinstance(h["score"], int)
        assert h["trigger_type"] == "Near 52W High"
        assert h["symbol"] == "TCS" and h["sector"] == "IT"
        assert (h["price"], h["change_pct"], h["prev_close"]) == (105.0, 5.0, 100.0)
        assert (h["rvol"], h["rvol_slope"], h["buy_pct"], h["dist_52w_pct"]) == (4.0, 0.0, 80.0, 5.0)
        assert h["trailing_stop"] == 100.47
        assert h["target_1"] == 110.25
        assert h["avg_traded_value"] == 26_250_000.0
        assert h["value_buy"] is True
        for alias in ("cmp", "ltp", "last_price", "close", "current_price"):
            assert h[alias] == 105.0
        assert h["market_note"] == "high buy_pct = strong signal"

    def test_score_over_65_but_small_move_is_only_building(self, sc, monkeypatch):
        h = scored(sc, flat_tick(price=101, open=101, high=101, low=101, vol_15m=35000, buy_pct=75),
                   static_row(daily_atr=0.0), monkeypatch=monkeypatch)
        assert h["score"] == 65
        assert h["tier"] == "building"
        assert h["trigger_type"] == "Near 52W High"

    def test_volume_shocker_override(self, sc, monkeypatch):
        h = scored(sc, flat_tick(price=105, open=105, high=105, low=105, vol_15m=20000),
                   flat_static(), monkeypatch=monkeypatch)
        assert h["score"] == 20
        assert h["tier"] == "breakout"
        assert h["trigger_type"] == "Volume Shocker"

    @pytest.mark.parametrize("price,vol", [(104.99, 20000), (105, 19900), (105, 0)])
    def test_shocker_needs_both_move_and_volume(self, sc, monkeypatch, price, vol):
        tick = flat_tick(price=price, open=price, high=price, low=price, vol_15m=vol)
        assert scored(sc, tick, flat_static(), monkeypatch=monkeypatch) is None

    def test_building_tier_renames_a_bare_consolidation(self, sc, monkeypatch):
        h = scored(sc, flat_tick(price=101, open=101, high=101, low=101, vol_15m=35000),
                   flat_static(), monkeypatch=monkeypatch)
        assert h["score"] == 35
        assert h["tier"] == "building"
        assert h["trigger_type"] == "Early Accumulation"

    def test_building_tier_keeps_a_specific_trigger(self, sc, monkeypatch):
        h = scored(sc, flat_tick(price=101, open=101, high=101, low=101, vol_15m=35000, buy_pct=75),
                   flat_static(), monkeypatch=monkeypatch)
        assert h["score"] == 50
        assert h["tier"] == "building"
        assert h["trigger_type"] == "Buy-Side Imbalance"

    def test_building_needs_a_move_above_the_floor(self, sc, monkeypatch):
        tick = flat_tick(price=100.3, open=100.3, high=100.3, low=100.3, vol_15m=35000)
        assert scored(sc, tick, flat_static(), monkeypatch=monkeypatch) is None

    def test_low_score_is_dropped(self, sc, monkeypatch):
        assert scored(sc, flat_tick(price=101, open=101, high=101, low=101), flat_static(),
                      monkeypatch=monkeypatch) is None

    def test_max_stock_price_cap(self, load, monkeypatch):
        m = load(MAX_STOCK_PRICE="100", **PERM_ENV)
        assert scored(m, flat_tick(price=100), flat_static(), monkeypatch=monkeypatch) is not None
        assert scored(m, flat_tick(price=100.01), flat_static(), monkeypatch=monkeypatch) is None

    @pytest.mark.parametrize("price,vb", [(19.99, False), (20.0, True), (500.0, True), (500.01, False)])
    def test_value_buy_badge(self, perm, price, vb):
        h = scored(perm, flat_tick(price=price, open=price, high=price, low=price), flat_static())
        assert h["value_buy"] is vb

    def test_value_buy_threshold_is_env_driven(self, load, monkeypatch):
        m = load(VALUE_BUY_THRESHOLD="150", **PERM_ENV)
        monkeypatch.setattr(m, "_session_progress_ist", lambda: 0.5)
        assert scored(m, flat_tick(price=150), flat_static())["value_buy"] is True
        assert scored(m, flat_tick(price=151), flat_static())["value_buy"] is False


class TestScoreStockLiquidityFloor:
    BREAKOUT = {"price": 105, "open": 100, "high": 106, "low": 100, "vol_15m": 40000, "buy_pct": 80}

    def test_illiquid_symbol_is_dropped_after_scoring(self, sc, caplog, monkeypatch):
        # 20 baseline 15m volume * 105 * 25 = 52,500 rupees/day  << 50 lakh
        tick = dict(self.BREAKOUT, vol_15m=800)
        with caplog.at_level(logging.DEBUG):
            assert scored(sc, tick, static_row(avg_15m_volume=20), monkeypatch=monkeypatch) is None
        assert "liquidity floor fail" in caplog.text

    def test_just_above_the_floor_is_kept(self, sc, monkeypatch):
        tick = {"price": 105, "open": 100, "high": 106, "low": 100, "vol_15m": 100000, "buy_pct": 80}
        h = scored(sc, tick, static_row(avg_15m_volume=1905), monkeypatch=monkeypatch)
        assert h is not None and h["avg_traded_value"] >= sc.HARD_FLOOR_LIQUIDITY

    def test_floor_is_env_driven(self, load, monkeypatch):
        m = load(HARD_FLOOR_LIQUIDITY="100000000")
        assert scored(m, self.BREAKOUT, static_row(), monkeypatch=monkeypatch) is None


# ── _tick_from_bulk_cache / _fetch_quote ──────────────────────────────────────

def data_feed_fake(monkeypatch, fn):
    m = types.ModuleType("data_feed")
    m.get_cached_quote = fn
    monkeypatch.setitem(sys.modules, "data_feed", m)
    return m


class TestTickFromBulkCache:
    def test_missing_data_feed_module(self, eng, monkeypatch):
        monkeypatch.setitem(sys.modules, "data_feed", None)
        assert eng._tick_from_bulk_cache("TCS") is None

    def test_lookup_error(self, eng, monkeypatch):
        data_feed_fake(monkeypatch, boom)
        assert eng._tick_from_bulk_cache("TCS") is None

    @pytest.mark.parametrize("row", [None, "x", [1], 5])
    def test_non_dict_row(self, eng, monkeypatch, row):
        data_feed_fake(monkeypatch, lambda s: row)
        assert eng._tick_from_bulk_cache("TCS") is None

    @pytest.mark.parametrize("row", [{}, {"price": 0}, {"volume": 100}])
    def test_row_without_price(self, eng, monkeypatch, row):
        data_feed_fake(monkeypatch, lambda s: row)
        assert eng._tick_from_bulk_cache("TCS") is None

    def test_maps_primary_fields(self, eng, monkeypatch):
        seen = []
        row = {"price": 101, "close": 100, "open": 99, "day_high": 103, "day_low": 98, "volume": 5000,
               "vwap": 100.5, "orb_high": 102, "vol_15m": 700, "buy_pct": 61, "total_bid_qty": 10,
               "total_ask_qty": 20}
        data_feed_fake(monkeypatch, lambda s: seen.append(s) or row)
        assert eng._tick_from_bulk_cache("TCS") == {
            "price": 101, "close": 100, "open": 99, "high": 103, "low": 98, "volume": 5000, "vwap": 100.5,
            "orb_high": 102, "vol_15m": 700, "buy_pct": 61, "total_bid_qty": 10, "total_ask_qty": 20,
            "_from_cache": True,
        }
        assert seen == ["TCS"]

    def test_alternate_field_names(self, eng, monkeypatch):
        row = {"ltp": 50, "high": 55, "low": 45, "buy_percentage": 70, "total_buy_quantity": 3,
               "total_sell_quantity": 4}
        data_feed_fake(monkeypatch, lambda s: row)
        t = eng._tick_from_bulk_cache("TCS")
        assert t["price"] == 50 and t["close"] == 50       # close falls back to price
        assert (t["high"], t["low"]) == (55, 45)
        assert (t["buy_pct"], t["total_bid_qty"], t["total_ask_qty"]) == (70, 3, 4)

    @pytest.mark.parametrize("key", ["price", "close", "cmp", "ltp"])
    def test_price_key_order(self, eng, monkeypatch, key):
        data_feed_fake(monkeypatch, lambda s: {key: 77})
        assert eng._tick_from_bulk_cache("TCS")["price"] == 77


class FakeClient:
    def __init__(self, resp=None, exc=None):
        self.resp = resp
        self.exc = exc
        self.calls = []

    async def get(self, url, timeout=None):
        self.calls.append((url, timeout))
        if self.exc is not None:
            raise self.exc
        return self.resp


class TestFetchQuote:
    def test_bulk_cache_hit_skips_http(self, eng, monkeypatch):
        monkeypatch.setattr(eng, "_tick_from_bulk_cache", lambda s: {"price": 1, "_from_cache": True})
        client = FakeClient(Resp(200, {"price": 9}))
        assert run(eng._fetch_quote(client, "http://md", "TCS")) == {"price": 1, "_from_cache": True}
        assert client.calls == []

    @pytest.fixture
    def nocache(self, eng, monkeypatch):
        monkeypatch.setattr(eng, "_tick_from_bulk_cache", lambda s: None)
        return eng

    def test_upstream_mapping(self, nocache, sc):
        body = {"price": 101, "close": 100, "open": 99, "high": 103, "low": 98, "volume": 5000, "vwap": 100,
                "orb_high": 102, "vol_15m": 700, "buy_pct": 61, "total_bid_qty": 10, "total_ask_qty": 20}
        client = FakeClient(Resp(200, body))
        out = run(nocache._fetch_quote(client, "http://md/", "TCS"))
        assert out == {"price": 101, "close": 100, "open": 99, "high": 103, "low": 98, "volume": 5000,
                       "vwap": 100, "orb_high": 102, "vol_15m": 700, "buy_pct": 61, "total_bid_qty": 10,
                       "total_ask_qty": 20}
        assert client.calls == [("http://md/quote/TCS", sc.QUOTE_TIMEOUT)]

    def test_url_trailing_slash_is_trimmed(self, nocache):
        client = FakeClient(Resp(200, {"price": 1}))
        run(nocache._fetch_quote(client, "http://md//", "TCS"))
        assert client.calls[0][0] == "http://md/quote/TCS"

    def test_yahoo_style_field_names(self, nocache):
        body = {"regularMarketPrice": 50, "regularMarketOpen": 49, "dayHigh": 52, "dayLow": 48,
                "regularMarketVolume": 900, "buy_percentage": 66, "total_buy_quantity": 7,
                "total_sell_quantity": 8}
        out = run(nocache._fetch_quote(FakeClient(Resp(200, body)), "http://md", "TCS"))
        assert out["price"] == 50 and out["close"] == 50
        assert (out["open"], out["high"], out["low"], out["volume"]) == (49, 52, 48, 900)
        assert (out["buy_pct"], out["total_bid_qty"], out["total_ask_qty"]) == (66, 7, 8)

    def test_regular_market_day_high_low_fallbacks(self, nocache):
        body = {"close": 10, "regularMarketDayHigh": 12, "regularMarketDayLow": 9}
        out = run(nocache._fetch_quote(FakeClient(Resp(200, body)), "http://md", "TCS"))
        assert out["price"] == 10 and (out["high"], out["low"]) == (12, 9)

    @pytest.mark.parametrize("status", [404, 500, 429])
    def test_non_200(self, nocache, status):
        assert run(nocache._fetch_quote(FakeClient(Resp(status, {"price": 1})), "http://md", "TCS")) is None

    @pytest.mark.parametrize("body", [None, [1, 2], "x"])
    def test_non_dict_body(self, nocache, body):
        assert run(nocache._fetch_quote(FakeClient(Resp(200, body)), "http://md", "TCS")) is None

    def test_http_error(self, nocache, caplog):
        with caplog.at_level(logging.DEBUG):
            assert run(nocache._fetch_quote(FakeClient(exc=RuntimeError("timeout")), "http://md", "TCS")) is None
        assert "quote TCS" in caplog.text


# ── scan ──────────────────────────────────────────────────────────────────────

def hit_row(sym, score=70, tier="breakout", sector=None):
    return {"symbol": sym, "score": score, "tier": tier, "sector": sector}


class ScanRig:
    """Wires a SurpriseStockEngine so scan() runs with no I/O, and records what it asked for."""

    def __init__(self, sc, monkeypatch, static=None, ticks=None, scores=None, n_static=None):
        self.sc = sc
        self.clock = Clock()
        monkeypatch.setattr(sc, "time", self.clock)
        self.e = sc.SurpriseStockEngine()
        self.static = static if static is not None else {}
        self.ticks = ticks or {}
        self.scores = scores or {}
        self.fetched = []
        self.scored = []
        self.loads = []
        self.n_static = n_static

        def load_static_cache(force=False):
            self.loads.append(force)
            self.e.static_cache = dict(self.static)
            return len(self.static) if self.n_static is None else self.n_static

        async def fetch(client, url, sym):
            self.fetched.append((url, sym))
            t = self.ticks.get(sym)
            if isinstance(t, Exception):
                raise t
            return t

        def score(sym, tick):
            self.scored.append((sym, tick))
            r = self.scores.get(sym)
            return dict(r) if r is not None else None

        monkeypatch.setattr(self.e, "load_static_cache", load_static_cache)
        monkeypatch.setattr(self.e, "_fetch_quote", fetch)
        monkeypatch.setattr(self.e, "score_stock", score)

    def scan(self, **kw):
        return run(self.e.scan(object(), "http://md", **kw))


@pytest.fixture
def rig(sc, monkeypatch, kv):
    def make(**kw):
        return ScanRig(sc, monkeypatch, **kw)
    return make


class TestScanCachedFastPath:
    def _prime(self, r, age=10.0, result=None):
        r.e._last_result = result or {"count": 1, "stocks": []}
        r.e._last_scan_ts = r.clock.now - age

    def test_fresh_in_memory_result_is_returned(self, rig):
        r = rig(static={"A": static_row()})
        self._prime(r, age=10.04)
        out = r.scan(cached=True)
        assert out["from_cache"] is True
        assert out["cache_age_sec"] == 10.0
        assert out["count"] == 1
        assert r.loads == [] and r.fetched == []
        assert "from_cache" not in r.e._last_result       # the stored dict was copied, not mutated

    def test_age_equal_to_limit_is_still_fresh(self, rig, sc):
        r = rig(static={"A": static_row()})
        self._prime(r, age=sc.SURPRISE_CACHE_MAX_AGE_SEC)
        assert r.scan(cached=True)["from_cache"] is True

    def test_stale_result_falls_through_to_a_live_scan(self, rig, sc):
        r = rig(static={"A": static_row()})
        self._prime(r, age=sc.SURPRISE_CACHE_MAX_AGE_SEC + 1)
        out = r.scan(cached=True)
        assert "from_cache" not in out
        assert r.loads == [False]

    def test_custom_max_age(self, rig):
        r = rig(static={"A": static_row()})
        self._prime(r, age=50)
        assert r.scan(cached=True, cached_max_age_sec=60)["from_cache"] is True
        assert "from_cache" not in r.scan(cached=True, cached_max_age_sec=40)

    def test_cold_process_reads_durable_cache_first(self, rig, kv, sc):
        r = rig(static={"A": static_row()})
        kv.store[sc.SURPRISE_LAST_RESULT_CACHE_KEY] = {"result": {"count": 9, "stocks": []},
                                                       "scan_ts": r.clock.now - 5}
        out = r.scan(cached=True)
        assert out["from_cache"] is True and out["count"] == 9
        assert out["cache_age_sec"] == 5.0
        assert r.loads == []

    def test_cold_process_with_empty_durable_cache_scans_live(self, rig):
        r = rig(static={"A": static_row()})
        out = r.scan(cached=True)
        assert "from_cache" not in out
        assert r.loads == [False]

    def test_cached_flag_is_ignored_when_symbols_are_given(self, rig):
        r = rig(static={"A": static_row()})
        self._prime(r)
        out = r.scan(cached=True, symbols=["A"])
        assert "from_cache" not in out
        assert r.loads == [False]

    def test_cache_is_not_consulted_without_the_flag(self, rig):
        r = rig(static={"A": static_row()})
        self._prime(r)
        assert "from_cache" not in r.scan()


class TestScanUniverse:
    def test_empty_static_feed_is_an_error_row(self, rig):
        r = rig(static={}, n_static=0)
        out = r.scan()
        assert out["count"] == 0 and out["stocks"] == [] and out["static_loaded"] == 0
        assert "premarket job first" in out["error"]
        assert out["elapsed_sec"] == 0.0
        assert r.fetched == []

    def test_force_reload_flag_is_forwarded(self, rig):
        r = rig(static={"A": static_row()})
        r.scan(force_reload_static=True)
        assert r.loads == [True]

    def test_default_universe_is_the_liquid_rows(self, rig):
        r = rig(static={"A": static_row(), "B": static_row(is_liquid=False), "C": static_row()})
        out = r.scan()
        assert [s for _, s in r.fetched] == ["A", "C"]
        assert out["universe_scanned"] == 2

    def test_row_without_is_liquid_counts_as_liquid(self, rig):
        row = static_row()
        del row["is_liquid"]
        r = rig(static={"A": row})
        r.scan()
        assert [s for _, s in r.fetched] == ["A"]

    def test_all_illiquid_falls_back_to_the_whole_cache(self, rig):
        r = rig(static={"A": static_row(is_liquid=False), "B": static_row(is_liquid=False)})
        out = r.scan()
        assert [s for _, s in r.fetched] == ["A", "B"]
        assert out["universe_scanned"] == 2

    def test_explicit_symbols_are_normalised_and_filtered(self, rig):
        r = rig(static={"TCS": static_row(), "INFY": static_row(), "HIDDEN": static_row(is_liquid=False)})
        out = r.scan(symbols=[" tcs.ns ", "INFY.BO", "", None, "NOPE", "hidden"])
        assert sorted(s for _, s in r.fetched) == ["HIDDEN", "INFY", "TCS"]
        assert out["universe_scanned"] == 3

    def test_market_data_url_is_forwarded(self, rig):
        r = rig(static={"A": static_row()})
        r.scan()
        assert r.fetched == [("http://md", "A")]

    def test_universe_is_scanned_in_chunks_of_25(self, rig, monkeypatch):
        r = rig(static={f"S{i:02d}": static_row() for i in range(30)})
        sizes = []
        real_gather = asyncio.gather

        async def spy(*aws, **kw):
            sizes.append(len(aws))
            return await real_gather(*aws, **kw)

        monkeypatch.setattr(asyncio, "gather", spy)
        r.scan()
        assert sizes == [25, 5]
        assert len(r.fetched) == 30


class TestScanTicks:
    def test_counters_per_outcome(self, rig):
        r = rig(static={c: static_row() for c in "ABCD"},
                ticks={"A": {"price": 1, "_from_cache": True}, "B": {"price": 2}, "C": RuntimeError("x"), "D": None})
        out = r.scan()
        assert out["quotes_ok"] == 2
        assert out["cache_hits"] == 1
        assert out["upstream_calls"] == 3       # B (live) + C (raised) + D (empty)

    def test_failed_and_empty_ticks_are_scored_as_empty_dicts(self, rig):
        r = rig(static={c: static_row() for c in "ABC"}, ticks={"A": RuntimeError("x"), "B": None, "C": {"price": 3}})
        r.scan()
        assert dict(r.scored) == {"A": {}, "B": {}, "C": {"price": 3}}

    def test_only_scored_hits_are_returned(self, rig):
        r = rig(static={"A": static_row(), "B": static_row()}, ticks={"A": {"price": 1}, "B": {"price": 1}},
                scores={"A": hit_row("A", 80)})
        assert [s["symbol"] for s in r.scan()["stocks"]] == ["A"]


class TestScanResult:
    def test_shape_sorting_and_counters(self, rig, sc):
        r = rig(static={c: static_row() for c in "ABCD"},
                ticks={c: {"price": 1} for c in "ABCD"},
                scores={"A": hit_row("A", 50, "building"), "B": hit_row("B", 90, "breakout"),
                        "C": hit_row("C", 70, "breakout"), "D": hit_row("D", 40, "building")})
        out = r.scan()
        assert [s["symbol"] for s in out["stocks"]] == ["B", "C", "A", "D"]
        assert out["count"] == 4
        assert out["breakout_count"] == 2 and out["building_count"] == 2
        assert out["static_loaded"] == 4 and out["universe_scanned"] == 4
        assert out["quotes_ok"] == 4 and out["upstream_calls"] == 4 and out["cache_hits"] == 0
        assert out["elapsed_sec"] == 0.0
        assert out["min_score"] == sc.MIN_SCORE
        assert out["min_change_pct"] == sc.MIN_CHANGE_PCT
        assert out["building_min_score"] == sc.BUILDING_MIN_SCORE
        assert out["market_note"] == "thresholds raised for quality"
        assert "Nifty" not in out["market_note"] and "Aug-2026" not in out["market_note"]

    def test_duplicate_symbols_are_deduped(self, rig):
        r = rig(static={"A": static_row(sector="IT"), "B": static_row(sector="IT")},
                ticks={"A": {"price": 1}, "B": {"price": 1}},
                scores={"A": hit_row("A", 90, sector="IT"), "B": hit_row("A", 70, sector="IT")})
        assert [s["score"] for s in r.scan()["stocks"]] == [90]

    def test_scan_ts_is_stamped(self, rig):
        r = rig(static={"A": static_row()})
        r.scan()
        assert r.e._last_scan_ts == r.clock.now


class TestScanSectorSympathy:
    def _rig(self, rig, scores, extra_ticks=None, n_peers=1):
        static = {"LEAD": static_row(sector="IT"), "OTHER": static_row(sector="BANK")}
        for i in range(n_peers):
            static[f"PEER{i:02d}"] = static_row(sector="IT", is_liquid=False)  # not in the main universe
        ticks = {"LEAD": {"price": 1}, "OTHER": {"price": 1}}
        ticks.update(extra_ticks or {})
        return rig(static=static, ticks=ticks, scores=scores)

    def test_peers_of_a_breakout_sector_are_rescored(self, rig, sc):
        r = self._rig(rig, {"LEAD": hit_row("LEAD", 90, "breakout", "IT"),
                            "PEER00": hit_row("PEER00", sc.SECTOR_SYMPATHY_MIN_SCORE, "building", "IT")},
                      extra_ticks={"PEER00": {"price": 5}})
        out = r.scan()
        peer = next(s for s in out["stocks"] if s["symbol"] == "PEER00")
        assert peer["trigger_type"] == "Sector Sympathy (IT)"
        assert ("PEER00", {"price": 5}) in r.scored
        assert not any(sym == "OTHER" and t == {} for sym, t in r.scored)  # other sectors are left alone

    def test_weak_peers_are_dropped(self, rig, sc):
        r = self._rig(rig, {"LEAD": hit_row("LEAD", 90, "breakout", "IT"),
                            "PEER00": hit_row("PEER00", sc.SECTOR_SYMPATHY_MIN_SCORE - 1, "building", "IT")})
        assert [s["symbol"] for s in r.scan()["stocks"]] == ["LEAD"]

    def test_unscorable_peers_are_dropped(self, rig):
        r = self._rig(rig, {"LEAD": hit_row("LEAD", 90, "breakout", "IT")})
        assert [s["symbol"] for s in r.scan()["stocks"]] == ["LEAD"]

    def test_peer_quote_errors_are_scored_as_empty(self, rig):
        r = self._rig(rig, {"LEAD": hit_row("LEAD", 90, "breakout", "IT"),
                            "PEER00": hit_row("PEER00", 60, "building", "IT")},
                      extra_ticks={"PEER00": RuntimeError("x")})
        out = r.scan()
        assert ("PEER00", {}) in r.scored
        assert any(s["symbol"] == "PEER00" for s in out["stocks"])

    def test_peer_with_empty_quote_is_scored_as_empty(self, rig):
        r = self._rig(rig, {"LEAD": hit_row("LEAD", 90, "breakout", "IT")}, extra_ticks={"PEER00": None})
        r.scan()
        assert ("PEER00", {}) in r.scored

    def test_peers_already_hit_are_not_refetched(self, rig):
        static = {"A": static_row(sector="IT"), "B": static_row(sector="IT")}
        r = rig(static=static, ticks={"A": {"price": 1}, "B": {"price": 1}},
                scores={"A": hit_row("A", 90, "breakout", "IT"), "B": hit_row("B", 60, "building", "IT")})
        r.scan()
        assert [s for _, s in r.fetched].count("B") == 1

    def test_only_breakouts_with_a_sector_trigger_the_pass(self, rig):
        static = {"A": static_row(sector="IT"), "B": static_row(sector="IT", is_liquid=False),
                  "C": static_row(sector=None), "D": static_row(sector="BANK")}
        r = rig(static=static, ticks={"A": {"price": 1}, "C": {"price": 1}, "D": {"price": 1}},
                scores={"A": hit_row("A", 50, "building", "IT"), "C": hit_row("C", 90, "breakout", None),
                        "D": hit_row("D", 50, "building", "BANK")})
        r.scan()
        assert [s for _, s in r.fetched] == ["A", "C", "D"]   # no second round for B

    def test_peer_pass_is_chunked(self, rig, monkeypatch):
        r = self._rig(rig, {"LEAD": hit_row("LEAD", 90, "breakout", "IT")}, n_peers=30)
        sizes = []
        real_gather = asyncio.gather

        async def spy(*aws, **kw):
            sizes.append(len(aws))
            return await real_gather(*aws, **kw)

        monkeypatch.setattr(asyncio, "gather", spy)
        r.scan()
        assert sizes == [2, 25, 5]


class TestScanLastResultPersistence:
    def test_full_scan_is_kept_in_memory_and_persisted(self, rig, kv, sc):
        r = rig(static={"A": static_row()}, ticks={"A": {"price": 1}}, scores={"A": hit_row("A")})
        out = r.scan()
        assert r.e._last_result is out
        key, payload, ttl = kv.sets[-1]
        assert key == sc.SURPRISE_LAST_RESULT_CACHE_KEY
        assert payload == {"result": out, "scan_ts": r.clock.now}
        # group132: kept for days (age is checked at read time), no longer ~340 s
        assert ttl == sc.SURPRISE_LAST_RESULT_TTL_DEFAULT_SEC == 7 * 24 * 3600

    def test_ttl_never_drops_below_the_max_age_floor(self, rig, kv, monkeypatch):
        monkeypatch.setenv("SURPRISE_LAST_RESULT_TTL_SEC", "60")
        r = rig(static={"A": static_row()})
        r.scan(cached_max_age_sec=30.9)
        assert kv.sets[-1][2] == 150          # max(60, 30 + 120)

    @pytest.mark.parametrize("raw,expected", [("", 604800), ("  ", 604800), ("junk", 604800),
                                              ("3600", 3600), ("3600.9", 3600), ("-5", 340), ("0", 340)])
    def test_ttl_env_parsing(self, sc, monkeypatch, raw, expected):
        monkeypatch.setenv("SURPRISE_LAST_RESULT_TTL_SEC", raw)
        assert sc._last_result_durable_ttl_sec(220) == expected

    def test_filtered_scans_are_not_cached(self, rig, kv):
        r = rig(static={"A": static_row()})
        r.scan(symbols=["A"])
        assert r.e._last_result is None
        assert kv.sets == []

    def test_persist_failure_is_swallowed(self, rig, kv, caplog):
        kv.set_raises = RuntimeError("neon down")
        r = rig(static={"A": static_row()})
        with caplog.at_level(logging.DEBUG):
            out = r.scan()
        assert r.e._last_result is out
        assert "durable cache set failed" in caplog.text

    def test_missing_kv_module_is_swallowed(self, rig, monkeypatch):
        monkeypatch.setitem(sys.modules, "kv_cache", None)
        r = rig(static={"A": static_row()})
        assert r.scan()["count"] == 0

    def test_round_trip_through_the_durable_cache(self, rig, kv):
        r1 = rig(static={"A": static_row()}, ticks={"A": {"price": 1}}, scores={"A": hit_row("A", 77)})
        first = r1.scan()
        r2 = rig(static={"A": static_row()})            # a "restarted" engine with an empty memory
        second = r2.scan(cached=True)
        assert second["from_cache"] is True
        assert second["stocks"] == first["stocks"]
        assert r2.loads == []


class TestScanEndToEnd:
    def test_real_scoring_through_the_real_fetch_path(self, sc, monkeypatch, kv):
        clock = Clock()
        monkeypatch.setattr(sc, "time", clock)
        monkeypatch.setattr(sc, "_session_progress_ist", lambda: 0.5)
        data_feed_fake(monkeypatch, lambda s: {"price": 105, "open": 100, "day_high": 106, "day_low": 100,
                                               "vol_15m": 40000, "buy_pct": 80} if s == "AAA" else None)
        e = sc.SurpriseStockEngine()
        monkeypatch.setattr(e, "load_static_cache", lambda force=False: (
            e.static_cache.update({"AAA": static_row(), "BBB": static_row(sector="BANK")}) or 2))
        client = FakeClient(Resp(404, {}))
        out = run(e.scan(client, "http://md"))
        assert [s["symbol"] for s in out["stocks"]] == ["AAA"]
        assert out["stocks"][0]["tier"] == "breakout"
        assert out["cache_hits"] == 1 and out["quotes_ok"] == 1
        assert out["upstream_calls"] == 1                       # BBB: the HTTP fallback returned nothing
        assert [c[0] for c in client.calls] == ["http://md/quote/BBB"]


# ── is_market_open_ist ────────────────────────────────────────────────────────

def holidays_fake(monkeypatch, fn):
    m = types.ModuleType("nse_holidays")
    m.is_nse_holiday = fn
    monkeypatch.setitem(sys.modules, "nse_holidays", m)


class TestIsMarketOpen:
    @pytest.fixture(autouse=True)
    def _no_holidays(self, monkeypatch):
        holidays_fake(monkeypatch, lambda d: False)

    @pytest.mark.parametrize("h,mi,want", [
        (9, 14, False), (9, 15, True), (12, 0, True), (15, 30, True), (15, 31, False), (2, 0, False),
    ])
    def test_weekday_session_window(self, sc, monkeypatch, h, mi, want):
        install_clock(monkeypatch, h=h, mi=mi)                  # Wed 2026-09-30
        assert sc.is_market_open_ist() is want

    @pytest.mark.parametrize("day", [3, 4])                      # Sat 3 Oct, Sun 4 Oct 2026
    def test_weekend_is_closed(self, sc, monkeypatch, day):
        install_clock(monkeypatch, mo=10, d=day, h=12)
        assert sc.is_market_open_ist() is False

    def test_exchange_holiday_is_closed(self, sc, monkeypatch):
        install_clock(monkeypatch, h=12)
        seen = []
        holidays_fake(monkeypatch, lambda d: seen.append(d) or True)
        assert sc.is_market_open_ist() is False
        assert seen == [_real_dt.date(2026, 9, 30)]

    def test_holiday_lookup_failure_is_ignored(self, sc, monkeypatch):
        install_clock(monkeypatch, h=12)
        holidays_fake(monkeypatch, boom)
        assert sc.is_market_open_ist() is True

    def test_missing_holiday_module_is_ignored(self, sc, monkeypatch):
        install_clock(monkeypatch, h=12)
        monkeypatch.setitem(sys.modules, "nse_holidays", None)
        assert sc.is_market_open_ist() is True

    def test_clock_failure_means_closed(self, sc, monkeypatch):
        monkeypatch.setitem(sys.modules, "zoneinfo", None)
        assert sc.is_market_open_ist() is False


# ── feed cache read / write ───────────────────────────────────────────────────

class TestFeedCacheRead:
    def test_dict_is_returned_as_is(self, sc, kv):
        kv.store[sc.SURPRISE_FEED_CACHE_KEY] = {"data": [1]}
        assert sc._read_surprise_feed_cache() == {"data": [1]}

    def test_json_string_is_parsed(self, sc, kv):
        kv.store[sc.SURPRISE_FEED_CACHE_KEY] = json.dumps({"data": [2]})
        assert sc._read_surprise_feed_cache() == {"data": [2]}

    @pytest.mark.parametrize("raw", ["[1, 2]", "42", "null", '"str"', "{not json", None, 7, [1]])
    def test_anything_else_reads_as_none(self, sc, kv, raw):
        kv.store[sc.SURPRISE_FEED_CACHE_KEY] = raw
        assert sc._read_surprise_feed_cache() is None

    def test_kv_error_reads_as_none(self, sc, kv, caplog):
        kv.kv_get_raises = RuntimeError("neon down")
        with caplog.at_level(logging.DEBUG):
            assert sc._read_surprise_feed_cache() is None
        assert "surprise feed cache read" in caplog.text

    def test_missing_kv_module_reads_as_none(self, sc, monkeypatch):
        monkeypatch.setitem(sys.modules, "kv_cache", None)
        assert sc._read_surprise_feed_cache() is None


class TestFeedCacheWrite:
    def test_market_closed_writes_without_ttl(self, sc, kv, monkeypatch):
        monkeypatch.setattr(sc, "is_market_open_ist", lambda: False)
        sc._write_surprise_feed_cache({"data": []})
        assert kv.kv_sets == [(sc.SURPRISE_FEED_CACHE_KEY, {"data": []}, None)]

    def test_market_open_uses_the_open_ttl(self, sc, kv, monkeypatch):
        monkeypatch.setattr(sc, "is_market_open_ist", lambda: True)
        sc._write_surprise_feed_cache({"data": []})
        assert kv.kv_sets[-1][2] == 7200

    def test_open_ttl_is_floored_at_an_hour(self, load, kv, monkeypatch):
        m = load(SURPRISE_FEED_OPEN_TTL_SEC="60")
        monkeypatch.setattr(m, "is_market_open_ist", lambda: True)
        m._write_surprise_feed_cache({})
        assert kv.kv_sets[-1][2] == 3600

    def test_write_failure_is_logged_not_raised(self, sc, kv, monkeypatch, caplog):
        monkeypatch.setattr(sc, "is_market_open_ist", lambda: False)
        kv.kv_set_raises = RuntimeError("neon down")
        with caplog.at_level(logging.WARNING):
            sc._write_surprise_feed_cache({})
        assert "surprise feed cache write" in caplog.text

    def test_missing_kv_module_is_logged_not_raised(self, sc, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "kv_cache", None)
        with caplog.at_level(logging.WARNING):
            sc._write_surprise_feed_cache({})
        assert "surprise feed cache write" in caplog.text


# ── run_market_aware_surprise_feed ────────────────────────────────────────────

class FSeries:
    def __init__(self, vals):
        self.vals = list(vals)

    @property
    def empty(self):
        return not self.vals

    def dropna(self):
        return FSeries([v for v in self.vals if v is not None])

    @property
    def iloc(self):
        return self.vals

    def __len__(self):
        return len(self.vals)


class FFrame:
    """Single-ticker OHLCV frame (flat columns, no `nlevels`)."""

    def __init__(self, **cols):
        self.cols = cols
        self.columns = list(cols)

    @property
    def empty(self):
        return not any(self.cols.values())

    def __getitem__(self, key):
        return FSeries(self.cols[key])


class FMulti:
    """yfinance group_by='ticker' frame."""

    def __init__(self, frames):
        self.frames = frames
        self.columns = SimpleNamespace(nlevels=2, get_level_values=lambda i: list(frames))

    def __getitem__(self, key):
        return self.frames[key]


def bars(closes=(99.0, 100.0), high=(101.0,), low=(98.0,), vol=(5000.0,)):
    return FFrame(Close=list(closes), High=list(high), Low=list(low), Volume=list(vol))


class FeedRig:
    """run_market_aware_surprise_feed() with a fake clock, engine, market state, yfinance and sleeps."""

    def __init__(self, sc, monkeypatch, kv):
        self.sc, self.mp, self.kv = sc, monkeypatch, kv
        self.clock = Clock()
        self.open = False
        self.loads = []
        self.engine = SimpleNamespace(static_cache={}, load_static_cache=lambda: self.loads.append(1))
        self.downloads = []
        self.results = []
        self.sleeps = []
        self.stop = [False]
        monkeypatch.setattr(sc, "time", self.clock)
        monkeypatch.setattr(sc, "is_market_open_ist", lambda: self.open)
        monkeypatch.setattr(sc, "surprise_engine", self.engine)
        monkeypatch.setitem(sys.modules, "yfinance", None)
        monkeypatch.setitem(sys.modules, "symbol_aliases", None)
        stop_mod = types.ModuleType("surprise_premarket")
        stop_mod.premarket_stop_requested = lambda: self.stop[0]
        monkeypatch.setitem(sys.modules, "surprise_premarket", stop_mod)
        real_sleep = asyncio.sleep

        async def fake_sleep(s, *a, **k):
            self.sleeps.append(s)
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def with_yfinance(self):
        yf = types.ModuleType("yfinance")

        def download(tickers, **kw):
            self.downloads.append((tickers, kw))
            r = self.results.pop(0) if self.results else None
            if isinstance(r, Exception):
                raise r
            return r

        yf.download = download
        self.mp.setitem(sys.modules, "yfinance", yf)

    def cache(self, age=None, data=(), **extra):
        row = {"timestamp": self.clock.now - age if age is not None else 0, "data": list(data)}
        row.update(extra)
        self.kv.store[self.sc.SURPRISE_FEED_CACHE_KEY] = row
        return row

    def waterfall(self, responses):
        """responses: {symbol: Resp | Exception}; returns the list of fake clients created."""
        clients = []

        class Client:
            def __init__(cs, **kw):
                cs.kw, cs.calls = kw, []
                clients.append(cs)

            async def __aenter__(cs):
                return cs

            async def __aexit__(cs, *a):
                return False

            async def get(cs, url):
                cs.calls.append(url)
                r = responses.get(url.rsplit("/", 1)[-1])
                if isinstance(r, Exception):
                    raise r
                return r if r is not None else Resp(404, {})

        import httpx
        self.mp.setattr(httpx, "AsyncClient", Client)
        return clients

    def go(self, **kw):
        return run(self.sc.run_market_aware_surprise_feed(**kw))


@pytest.fixture
def feed(sc, monkeypatch, kv):
    return FeedRig(sc, monkeypatch, kv)


class TestFeedCacheShortCircuits:
    def test_market_closed_serves_the_durable_cache(self, feed):
        feed.cache(age=30, data=[{"symbol": "A"}])
        out = feed.go()
        assert out == {"status": "success", "source": "cache", "market_open": False, "age_sec": 30,
                       "data": [{"symbol": "A"}], "message": "Market closed — serving durable cache (no API calls)"}

    def test_cache_without_data_key_serves_an_empty_list(self, feed):
        feed.kv.store[feed.sc.SURPRISE_FEED_CACHE_KEY] = {"timestamp": feed.clock.now - 5}
        assert feed.go()["data"] == []

    def test_open_market_fresh_cache_is_a_hit(self, feed):
        feed.open = True
        feed.cache(age=100, data=[{"symbol": "A"}])
        out = feed.go()
        assert out["source"] == "cache" and out["market_open"] is True and out["age_sec"] == 100
        assert out["message"] == "Cache hit (100s old, TTL 7200s)"

    def test_open_market_stale_cache_goes_live(self, feed):
        feed.open = True
        feed.cache(age=7200, data=[{"symbol": "A"}])
        assert feed.go(symbols=[]) ["source"] == "live"

    def test_force_reuses_a_very_recent_cache(self, feed):
        feed.open = True
        feed.cache(age=299, data=[{"symbol": "A"}])
        out = feed.go(force=True)
        assert out["source"] == "cache" and out["market_open"] is True
        assert out["message"] == "force=true but cache is only 299s old — reused"

    def test_force_with_an_older_cache_goes_live(self, feed):
        feed.cache(age=300)
        assert feed.go(force=True)["source"] == "live"

    def test_force_bypasses_the_closed_market_cache(self, feed):
        feed.cache(age=4000)
        assert feed.go(force=True)["source"] == "live"

    def test_missing_timestamp_reads_as_an_ancient_cache(self, feed):
        feed.open = True
        feed.kv.store[feed.sc.SURPRISE_FEED_CACHE_KEY] = {"data": [{"symbol": "A"}]}
        assert feed.go()["source"] == "live"

    def test_empty_cache_is_ignored(self, feed):
        feed.kv.store[feed.sc.SURPRISE_FEED_CACHE_KEY] = {}
        assert feed.go()["source"] == "live"


class TestFeedSymbolResolution:
    def test_no_symbols_anywhere_is_an_error(self, feed):
        out = feed.go()
        assert out == {"status": "error", "source": "live", "data": [],
                       "message": "No symbols — run premarket baselines first"}
        assert feed.loads == [1]
        assert feed.kv.kv_sets == []

    def test_uses_the_loaded_static_cache_without_reloading(self, feed):
        feed.engine.static_cache = {"AAA": {}, "BBB": {}}
        feed.with_yfinance()
        feed.results = [FMulti({"AAA.NS": bars(), "BBB.NS": bars()})]
        out = feed.go()
        assert feed.loads == []
        assert feed.downloads[0][0] == "AAA.NS BBB.NS"
        assert out["count"] == 2

    def test_loads_the_static_cache_when_empty(self, feed):
        feed.engine.load_static_cache = lambda: feed.engine.static_cache.update({"AAA": {}})
        feed.with_yfinance()
        feed.results = [FMulti({"AAA.NS": bars()})]
        assert feed.go()["count"] == 1

    def test_static_cache_load_failure_means_no_symbols(self, feed):
        feed.engine.load_static_cache = boom
        assert feed.go()["status"] == "error"

    def test_explicit_symbols_are_normalised_and_deduped(self, feed):
        feed.with_yfinance()
        feed.results = [None]
        feed.go(symbols=[" tcs.ns ", "TCS", "infy.bo", "", None, 0])
        assert feed.downloads[0][0] == "TCS.NS INFY.NS"
        assert feed.loads == []

    def test_all_blank_explicit_symbols_fall_back_to_the_engine(self, feed):
        feed.engine.static_cache = {"AAA": {}}
        feed.with_yfinance()
        feed.results = [None]
        feed.go(symbols=["", None])
        assert feed.downloads[0][0] == "AAA.NS"


class TestFeedSymbolCleaning:
    def _tickers(self, feed, syms):
        feed.with_yfinance()
        feed.results = [None]
        feed.go(symbols=syms)
        return feed.downloads[0][0].split()

    def test_known_renames_and_suffix_rules(self, feed):
        got = self._tickers(feed, ["kfin technologies", "PB FINTECH", "360 ONE", "Foo Technologies", "Foo Technology",
                                   "Bar Ltd", "Baz Limited", "A B C", "X%20Y", "HONASA CONSUMER"])
        # "Foo Technology" cleans to the same FOOTECH, so it collapses into the first one
        assert got == ["KFINTECH.NS", "POLICYBZR.NS", "360ONE.NS", "FOOTECH.NS", "BAR.NS", "BAZ.NS", "ABC.NS",
                       "XY.NS", "HONASA.NS"]

    def test_derivative_contracts_are_dropped(self, feed):
        assert self._tickers(feed, ["TCS", "APLAPOLLO29SEP26FUT", "BANKNIFTY29SEP2648000CE"]) == ["TCS.NS"]

    def test_all_dropped_falls_back_to_the_raw_list(self, feed):
        assert self._tickers(feed, ["X29SEP26FUT"]) == ["X29SEP26FUT.NS"]

    def test_duplicates_after_cleaning_collapse(self, feed):
        assert self._tickers(feed, ["Foo Ltd", "FOO", "foo.ns"]) == ["FOO.NS"]

    def test_alias_module_drops_delisted_and_renames(self, feed, monkeypatch):
        alias = types.ModuleType("symbol_aliases")
        alias.is_known_delisted = lambda u: u == "DEAD"
        alias.resolve_base_symbol = lambda u: {"OLD": "NEW", "GONE": None}.get(u, u)
        monkeypatch.setitem(sys.modules, "symbol_aliases", alias)
        assert self._tickers(feed, ["DEAD", "OLD", "GONE", "TCS"]) == ["NEW.NS", "TCS.NS"]

    def test_alias_module_failure_leaves_the_symbol_alone(self, feed, monkeypatch):
        alias = types.ModuleType("symbol_aliases")
        alias.is_known_delisted = boom
        alias.resolve_base_symbol = boom
        monkeypatch.setitem(sys.modules, "symbol_aliases", alias)
        assert self._tickers(feed, ["TCS"]) == ["TCS.NS"]


class TestFeedYahooChunks:
    def test_download_arguments(self, feed):
        feed.with_yfinance()
        feed.results = [None]
        feed.go(symbols=["A", "B"])
        assert feed.downloads[0][1] == {"period": "2d", "group_by": "ticker", "threads": True,
                                        "progress": False, "auto_adjust": True}

    def test_row_shape_and_payload_written(self, feed):
        feed.with_yfinance()
        feed.results = [FMulti({"AAA.NS": bars(closes=(99.0, 100.0), high=(103.0,), low=(97.0,), vol=(5000.0,))})]
        out = feed.go(symbols=["AAA"])
        assert out["status"] == "success" and out["source"] == "live" and out["count"] == 1 and out["errors"] == 0
        assert out["message"] == "Live surprise feed: 1 quotes, 0 errors"
        assert out["data"] == [{"symbol": "AAA", "price": 100.0, "cmp": 100.0, "previous_close": 99.0,
                                "day_change_pct": 1.01, "day_high": 103.0, "day_low": 97.0,
                                "source": "yahoo_bulk", "volume": 5000}]
        key, payload, ttl = feed.kv.kv_sets[-1]
        assert key == feed.sc.SURPRISE_FEED_CACHE_KEY and ttl is None
        assert payload == {"timestamp": feed.clock.now, "data": out["data"], "count": 1, "errors": 0,
                           "market_open": False}

    def test_single_symbol_chunk_uses_the_flat_frame(self, feed):
        feed.with_yfinance()
        feed.results = [bars()]
        assert feed.go(symbols=["AAA"])["count"] == 1

    def test_flat_frame_for_several_symbols_yields_nothing(self, feed):
        feed.with_yfinance()
        feed.results = [bars()]
        assert feed.go(symbols=["AAA", "BBB"])["count"] == 0

    def test_none_frame_yields_nothing(self, feed):
        feed.with_yfinance()
        feed.results = [None]
        assert feed.go(symbols=["AAA"])["count"] == 0

    def test_symbol_missing_from_the_frame_is_skipped(self, feed):
        feed.with_yfinance()
        feed.results = [FMulti({"AAA.NS": bars()})]
        out = feed.go(symbols=["AAA", "BBB"])
        assert [r["symbol"] for r in out["data"]] == ["AAA"]

    @pytest.mark.parametrize("frame", [
        FFrame(Close=[]),                                   # empty frame
        FFrame(Open=[1.0]),                                 # no Close column
        FFrame(Close=[None, None]),                         # Close is all-NaN
        bars(closes=(0.0,)),                                # non-positive price
    ])
    def test_unusable_frames_are_skipped_without_error(self, feed, frame):
        feed.with_yfinance()
        feed.results = [FMulti({"AAA.NS": frame})]
        out = feed.go(symbols=["AAA"])
        assert out["count"] == 0 and out["errors"] == 0

    def test_price_above_the_cap_is_skipped(self, load, monkeypatch, kv):
        m = load(MAX_STOCK_PRICE="100")
        rig = FeedRig(m, monkeypatch, kv)
        rig.with_yfinance()
        rig.results = [FMulti({"CHEAP.NS": bars(closes=(99.0, 100.0)), "DEAR.NS": bars(closes=(99.0, 100.5))})]
        assert [r["symbol"] for r in rig.go(symbols=["CHEAP", "DEAR"])["data"]] == ["CHEAP"]

    def test_single_bar_uses_the_price_as_previous_close(self, feed):
        feed.with_yfinance()
        feed.results = [FMulti({"AAA.NS": bars(closes=(100.0,))})]
        row = feed.go(symbols=["AAA"])["data"][0]
        assert row["previous_close"] == 100.0 and row["day_change_pct"] == 0.0

    def test_non_positive_previous_close_drops_the_change(self, feed):
        feed.with_yfinance()
        feed.results = [FMulti({"AAA.NS": bars(closes=(0.0, 100.0))})]
        row = feed.go(symbols=["AAA"])["data"][0]
        assert row["previous_close"] == 0.0 and "day_change_pct" not in row

    def test_missing_ohlv_columns_are_left_out(self, feed):
        feed.with_yfinance()
        feed.results = [FMulti({"AAA.NS": FFrame(Close=[99.0, 100.0])})]
        row = feed.go(symbols=["AAA"])["data"][0]
        assert not {"day_high", "day_low", "volume"} & set(row)

    def test_negative_volume_is_dropped(self, feed):
        feed.with_yfinance()
        feed.results = [FMulti({"AAA.NS": bars(vol=(-5.0,))})]
        assert "volume" not in feed.go(symbols=["AAA"])["data"][0]

    def test_unparseable_ohlv_is_ignored(self, feed):
        feed.with_yfinance()
        feed.results = [FMulti({"AAA.NS": bars(high=("x",), low=(98.0,), vol=("y",))})]
        row = feed.go(symbols=["AAA"])["data"][0]
        assert row["price"] == 100.0 and "day_high" not in row and "volume" not in row

    def test_a_bad_symbol_counts_one_error_and_the_rest_continue(self, feed):
        feed.with_yfinance()
        feed.results = [FMulti({"AAA.NS": bars(closes=("abc",)), "BBB.NS": bars()})]
        out = feed.go(symbols=["AAA", "BBB"])
        assert out["errors"] == 1 and [r["symbol"] for r in out["data"]] == ["BBB"]

    def test_download_failure_counts_an_error(self, feed, caplog):
        feed.with_yfinance()
        feed.results = [RuntimeError("429")]
        with caplog.at_level(logging.WARNING):
            out = feed.go(symbols=["AAA"])
        assert out["errors"] == 1 and out["count"] == 0
        assert "surprise yf chunk 0" in caplog.text

    def test_missing_yfinance_is_logged_and_everything_misses(self, feed, caplog):
        with caplog.at_level(logging.WARNING):
            out = feed.go(symbols=["AAA"])
        assert out["count"] == 0
        assert "surprise chunked yf unavailable" in caplog.text

    def test_symbols_go_out_in_chunks_of_50_with_a_pause_each(self, feed):
        feed.with_yfinance()
        feed.results = [None, None]
        feed.go(symbols=[f"S{i:03d}" for i in range(51)])
        assert [len(t.split()) for t, _ in feed.downloads] == [50, 1]
        assert feed.sleeps.count(0.5) == 2

    def test_stop_request_ends_the_chunk_loop(self, feed):
        feed.with_yfinance()
        feed.stop[0] = True
        feed.go(symbols=["AAA"])
        assert feed.downloads == []

    def test_missing_premarket_module_means_never_stop(self, feed, monkeypatch):
        feed.with_yfinance()
        feed.results = [None]
        monkeypatch.setitem(sys.modules, "surprise_premarket", None)
        feed.go(symbols=["AAA"])
        assert len(feed.downloads) == 1

    def test_open_market_payload_carries_the_open_flag_and_ttl(self, feed):
        feed.open = True
        feed.go(symbols=["AAA"])
        _, payload, ttl = feed.kv.kv_sets[-1]
        assert payload["market_open"] is True and ttl == 7200


class TestFeedWaterfall:
    def _go(self, feed, responses, md="http://md/", **kw):
        clients = feed.waterfall(responses)
        out = feed.go(symbols=["AAA"], market_data_url=md, **kw)
        return out, clients

    def test_fills_a_yahoo_miss_from_market_data(self, feed):
        out, clients = self._go(feed, {"AAA": Resp(200, {"cmp": 50.5, "source": "angel"})})
        assert out["data"] == [{"symbol": "AAA", "price": 50.5, "cmp": 50.5, "source": "angel"}]
        assert clients[0].calls == ["http://md/quote/AAA"]
        assert clients[0].kw == {"timeout": 8.0, "follow_redirects": True}
        assert feed.sleeps[-1] == feed.sc.SURPRISE_FEED_COOLDOWN_SEC

    def test_source_defaults_to_waterfall(self, feed):
        assert self._go(feed, {"AAA": Resp(200, {"price": 5})})[0]["data"][0]["source"] == "waterfall"

    def test_price_key_order_skips_bad_values(self, feed):
        out, _ = self._go(feed, {"AAA": Resp(200, {"price": "abc", "cmp": 0, "ltp": None, "last_price": 9})})
        assert out["data"][0]["price"] == 9.0

    def test_no_market_data_url_means_no_waterfall(self, feed):
        clients = feed.waterfall({})
        out = feed.go(symbols=["AAA"], market_data_url="")
        assert clients == [] and out["count"] == 0

    def test_symbols_already_found_by_yahoo_are_not_requested(self, feed):
        feed.with_yfinance()
        feed.results = [FMulti({"AAA.NS": bars()})]
        clients = feed.waterfall({})
        feed.go(symbols=["AAA"], market_data_url="http://md")
        assert clients == []

    @pytest.mark.parametrize("resp", [Resp(200, {}), Resp(200, {"price": -1}), Resp(200, ["x"]), Resp(404, {}),
                                      Resp(500, {"price": 5})])
    def test_unusable_responses_add_nothing_and_no_error(self, feed, resp):
        out, _ = self._go(feed, {"AAA": resp})
        assert out["count"] == 0 and out["errors"] == 0

    @pytest.mark.parametrize("status", [401, 429])
    def test_auth_and_rate_limit_back_off(self, feed, status):
        out, _ = self._go(feed, {"AAA": Resp(status, {})})
        assert out["errors"] == 1
        assert feed.sc.SURPRISE_FEED_COOLDOWN_SEC * 2 in feed.sleeps

    def test_request_error_counts_and_continues(self, feed):
        out, _ = self._go(feed, {"AAA": RuntimeError("timeout")})
        assert out["errors"] == 1 and out["count"] == 0

    def test_price_above_the_cap_is_skipped(self, load, monkeypatch, kv):
        m = load(MAX_STOCK_PRICE="100")
        rig = FeedRig(m, monkeypatch, kv)
        rig.waterfall({"AAA": Resp(200, {"price": 101})})
        assert rig.go(symbols=["AAA"], market_data_url="http://md")["count"] == 0

    def test_stop_request_ends_the_waterfall(self, feed):
        clients = feed.waterfall({})
        feed.stop[0] = True
        feed.go(symbols=["AAA", "BBB"], market_data_url="http://md")
        assert clients[0].calls == []

    def test_missing_premarket_module_means_never_stop(self, feed, monkeypatch):
        clients = feed.waterfall({"AAA": Resp(200, {"price": 5})})
        monkeypatch.setitem(sys.modules, "surprise_premarket", None)
        assert feed.go(symbols=["AAA"], market_data_url="http://md")["count"] == 1
        assert clients[0].calls == ["http://md/quote/AAA"]


# ── audit_surprise_feed ───────────────────────────────────────────────────────

class TestAuditSurpriseFeed:
    def test_no_cache(self, sc, kv, monkeypatch):
        monkeypatch.setattr(sc, "is_market_open_ist", lambda: False)
        out = sc.audit_surprise_feed()
        assert out == {"ok": True, "total_tracked": 0, "fully_populated": 0, "missing_data": 0,
                       "health_score": 0.0, "incomplete_stocks": [], "cache_age_sec": None,
                       "market_open": False, "source": "empty",
                       "thresholds": {"min_score": 65, "min_change_pct": 1.5, "rvol_slope_min": 0.6}}

    def test_counts_complete_and_missing_rows(self, sc, kv, monkeypatch):
        clock = Clock()
        monkeypatch.setattr(sc, "time", clock)
        monkeypatch.setattr(sc, "is_market_open_ist", lambda: True)
        kv.store[sc.SURPRISE_FEED_CACHE_KEY] = {"timestamp": clock.now - 42.9, "data": [
            {"symbol": "a", "price": 10}, {"symbol": "b", "cmp": 5}, {"symbol": "c", "ltp": 1},
            {"symbol": "d", "close": 2}, {"symbol": "e", "price": 0}, {"symbol": "f", "price": "abc"},
            {"symbol": None}, "not-a-dict",
        ]}
        out = sc.audit_surprise_feed()
        assert (out["total_tracked"], out["fully_populated"], out["missing_data"]) == (8, 4, 3)
        assert out["incomplete_stocks"] == [{"symbol": "E", "missing_fields": ["price"]},
                                            {"symbol": "F", "missing_fields": ["price"]},
                                            {"symbol": "", "missing_fields": ["price"]}]
        assert out["health_score"] == 50.0
        assert out["cache_age_sec"] == 42 and out["market_open"] is True and out["source"] == "cache"

    def test_incomplete_list_is_capped_at_200(self, sc, kv, monkeypatch):
        monkeypatch.setattr(sc, "is_market_open_ist", lambda: False)
        kv.store[sc.SURPRISE_FEED_CACHE_KEY] = {"timestamp": 1, "data": [{"symbol": f"S{i}"} for i in range(250)]}
        out = sc.audit_surprise_feed()
        assert out["missing_data"] == 250 and len(out["incomplete_stocks"]) == 200
        assert out["health_score"] == 0.0

    def test_health_score_is_rounded(self, sc, kv, monkeypatch):
        monkeypatch.setattr(sc, "is_market_open_ist", lambda: False)
        kv.store[sc.SURPRISE_FEED_CACHE_KEY] = {"timestamp": 1, "data": [{"price": 1}, {"price": 1}, {}]}
        assert sc.audit_surprise_feed()["health_score"] == 66.7

    def test_non_list_data_counts_as_empty(self, sc, kv, monkeypatch):
        monkeypatch.setattr(sc, "is_market_open_ist", lambda: False)
        kv.store[sc.SURPRISE_FEED_CACHE_KEY] = {"timestamp": 1, "data": "oops"}
        out = sc.audit_surprise_feed()
        assert out["total_tracked"] == 0 and out["source"] == "cache"

    def test_corrupt_non_dict_cache_reads_as_empty(self, sc, kv, monkeypatch):
        # regression: a JSON list in the cache used to crash on `cached.get(...)`
        monkeypatch.setattr(sc, "is_market_open_ist", lambda: False)
        kv.store[sc.SURPRISE_FEED_CACHE_KEY] = "[1, 2]"
        out = sc.audit_surprise_feed()
        assert out["source"] == "empty" and out["cache_age_sec"] is None


# ── repair_surprise_batch ─────────────────────────────────────────────────────

class RepairRig:
    def __init__(self, sc, monkeypatch, kv):
        self.sc, self.mp, self.kv = sc, monkeypatch, kv
        self.clock = Clock()
        self.calls = []
        self.kw = None
        self.raises = None
        self.responses = {}
        monkeypatch.setattr(sc, "time", self.clock)
        monkeypatch.setattr(sc, "is_market_open_ist", lambda: False)
        rig = self

        class Client:
            def __init__(cs, **kw):
                if rig.raises is not None:
                    raise rig.raises
                rig.kw = kw

            def __enter__(cs):
                return cs

            def __exit__(cs, *a):
                return False

            def get(cs, url):
                rig.calls.append(url)
                r = rig.responses.get(url.rsplit("/", 1)[-1])
                if isinstance(r, Exception):
                    raise r
                return r if r is not None else Resp(404, {})

        import httpx
        monkeypatch.setattr(httpx, "Client", Client)

    def seed(self, data, **extra):
        row = {"timestamp": 1.0, "data": data}
        row.update(extra)
        self.kv.store[self.sc.SURPRISE_FEED_CACHE_KEY] = row

    def saved(self):
        return self.kv.kv_sets[-1][1]

    def go(self, **kw):
        return self.sc.repair_surprise_batch(**kw)


@pytest.fixture
def repair(sc, monkeypatch, kv):
    return RepairRig(sc, monkeypatch, kv)


class TestRepairSurpriseBatch:
    def test_no_cache_or_no_rows(self, repair):
        assert repair.go() == {"status": "no_data", "repaired": []}
        repair.seed([])
        assert repair.go() == {"status": "no_data", "repaired": []}
        assert repair.kv.kv_sets == []

    def test_nothing_missing(self, repair):
        repair.seed([{"symbol": "A", "price": 10}, "junk", {"symbol": "", "price": 0}, {"symbol": None}])
        assert repair.go(market_data_url="http://md") == {"status": "completed", "repaired": [],
                                                          "message": "Nothing missing"}
        assert repair.calls == []

    def test_repairs_rows_that_lack_a_price(self, repair):
        repair.seed([{"symbol": "a", "price": 0}, {"symbol": "B", "price": 10}, {"symbol": "c", "price": "abc"}],
                    keep="me")
        repair.responses = {"A": Resp(200, {"ltp": 55.5, "source": "angel"}), "C": Resp(200, {"price": 7})}
        out = repair.go(market_data_url="http://md/")
        assert out == {"status": "completed", "repaired": ["A", "C"], "repaired_count": 2, "targets": ["A", "C"]}
        assert repair.calls == ["http://md/quote/A", "http://md/quote/C"]
        assert repair.kw == {"timeout": 8.0, "follow_redirects": True}
        saved = repair.saved()
        assert saved["keep"] == "me" and saved["timestamp"] == repair.clock.now
        a, b, c = saved["data"]
        assert (a["price"], a["cmp"], a["source"]) == (55.5, 55.5, "angel")
        assert b == {"symbol": "B", "price": 10}
        assert (c["price"], c["source"]) == (7.0, "waterfall")

    @pytest.mark.parametrize("limit,want", [(1, 1), (2, 2), (0, 15), (None, 15), (1000, 20), (-5, 1)])
    def test_limit_clamping(self, repair, limit, want):
        repair.seed([{"symbol": f"S{i}", "price": 0} for i in range(20)])
        assert len(repair.go(limit=limit, market_data_url="http://md")["targets"]) == want

    def test_limit_is_capped_at_100(self, repair):
        repair.seed([{"symbol": f"S{i}", "price": 0} for i in range(130)])
        assert len(repair.go(limit=500, market_data_url="http://md")["targets"]) == 100

    def test_forced_symbol_already_in_the_cache(self, repair):
        repair.seed([{"symbol": "AAA", "price": 5}, {"symbol": "BBB", "price": 0}])
        repair.responses = {"AAA": Resp(200, {"price": 6})}
        out = repair.go(symbol=" aaa.ns ", market_data_url="http://md")
        assert out["targets"] == ["AAA"] and out["repaired"] == ["AAA"]
        assert repair.saved()["data"][0]["price"] == 6.0

    def test_forced_symbol_missing_from_the_cache_is_appended(self, repair):
        repair.seed([{"symbol": "BBB", "price": 5}])
        repair.responses = {"NEW": Resp(200, {"price": 9})}
        out = repair.go(symbol="new", market_data_url="http://md")
        assert out["repaired"] == ["NEW"]
        assert repair.saved()["data"][-1] == {"symbol": "NEW", "price": 9.0, "cmp": 9.0, "source": "waterfall"}

    def test_blank_forced_symbol_is_ignored(self, repair):
        repair.seed([{"symbol": "A", "price": 0}])
        repair.responses = {"A": Resp(200, {"price": 1})}
        assert repair.go(symbol="  .NS ", market_data_url="http://md")["targets"] == ["A"]

    def test_market_data_url_falls_back_to_the_environment(self, repair, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_URL", "http://env-md/")
        repair.seed([{"symbol": "A", "price": 0}])
        repair.go()
        assert repair.calls == ["http://env-md/quote/A"]

    @pytest.mark.parametrize("resp", [Resp(200, {}), Resp(200, ["x"]), Resp(404, {"price": 5}), Resp(500, {})])
    def test_unusable_responses_leave_the_row_alone(self, repair, resp):
        repair.seed([{"symbol": "A", "price": 0}])
        repair.responses = {"A": resp}
        out = repair.go(market_data_url="http://md")
        assert out["repaired"] == [] and repair.saved()["data"] == [{"symbol": "A", "price": 0}]

    def test_price_above_the_cap_is_not_applied(self, load, monkeypatch, kv):
        m = load(MAX_STOCK_PRICE="100")
        rig = RepairRig(m, monkeypatch, kv)
        rig.seed([{"symbol": "A", "price": 0}])
        rig.responses = {"A": Resp(200, {"price": 101})}
        assert rig.go(market_data_url="http://md")["repaired"] == []

    def test_each_symbol_is_followed_by_a_cooldown(self, repair):
        repair.seed([{"symbol": "A", "price": 0}, {"symbol": "B", "price": 0}])
        repair.responses = {"A": Resp(200, {"price": 1}), "B": Resp(200, {"price": 2})}
        repair.go(market_data_url="http://md")
        assert repair.clock.sleeps == [repair.sc.SURPRISE_FEED_COOLDOWN_SEC] * 2

    def test_a_skipped_price_cools_down_once(self, repair):
        # the `continue` after the skip's own sleep bypasses the loop-end sleep, so it is one pause, not two
        repair.seed([{"symbol": "A", "price": 0}])
        repair.responses = {"A": Resp(200, {})}
        repair.go(market_data_url="http://md")
        assert repair.clock.sleeps == [repair.sc.SURPRISE_FEED_COOLDOWN_SEC]

    def test_price_key_order_skips_bad_values(self, repair):
        repair.seed([{"symbol": "A", "price": 0}])
        repair.responses = {"A": Resp(200, {"price": "abc", "cmp": 0, "ltp": None, "close": 4})}
        assert repair.go(market_data_url="http://md")["repaired"] == ["A"]
        assert repair.saved()["data"][0]["price"] == 4.0

    def test_per_symbol_errors_are_swallowed(self, repair):
        repair.seed([{"symbol": "A", "price": 0}, {"symbol": "B", "price": 0}])
        repair.responses = {"A": RuntimeError("timeout"), "B": Resp(200, {"price": 3})}
        assert repair.go(market_data_url="http://md")["repaired"] == ["B"]

    def test_client_failure_is_logged_and_the_cache_still_saved(self, repair, caplog):
        repair.seed([{"symbol": "A", "price": 0}])
        repair.raises = RuntimeError("no client")
        with caplog.at_level(logging.WARNING):
            out = repair.go(market_data_url="http://md")
        assert out["repaired"] == [] and out["status"] == "completed"
        assert "repair_surprise_batch" in caplog.text
        assert len(repair.kv.kv_sets) == 1


# ── group118: delisted symbols never enter the static cache ──────────────────

class TestStaticCacheSkipsDelisted:
    def test_table_rows_for_delisted_symbols_are_dropped(self, db_setup):
        e, db, _ = db_setup
        db.rows = [db_row("AAKASH"), db_row("TCS"), db_row(" annapurna "), db_row("TATAMTRDVR")]
        assert e.load_static_cache() == 1
        assert set(e.static_cache) == {"TCS"}

    def test_dead_rows_are_never_quoted_or_scored(self, db_setup):
        e, db, _ = db_setup
        db.rows = [db_row("AAKASH"), db_row("INFY")]
        e.load_static_cache()
        assert [k for k, v in e.static_cache.items() if v.get("is_liquid", True)] == ["INFY"]

    def test_unimportable_symbol_aliases_keeps_every_row(self, db_setup, monkeypatch):
        e, db, _ = db_setup
        monkeypatch.setitem(sys.modules, "symbol_aliases", None)  # import raises ImportError
        db.rows = [db_row("AAKASH"), db_row("TCS")]
        assert e.load_static_cache() == 2

    def test_is_dead_symbol_helper(self, sc):
        assert sc._is_dead_symbol("AAKASH") is True
        assert sc._is_dead_symbol("annapurna.ns") is True
        assert sc._is_dead_symbol("RELIANCE") is False
        assert sc._is_dead_symbol("") is False


class TestSeedSkipsDelisted:
    def test_kv_seed_skips_delisted_keys(self, seeder):
        sc, db, e = seeder
        good = {"price": 10}
        db.rows = [("feed:AAKASH", good), ("stockky:data_feed:ANNAPURNA.NS", good), ("feed:TCS", good)]
        cache = {}
        assert e._seed_from_data_feed_kv(cache) == 1
        assert set(cache) == {"TCS"}
