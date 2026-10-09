"""tests/test_surprise_premarket.py — coverage for api-gateway/surprise_premarket.py

The pre-market baseline job: env-driven limits, DB URL / engine / dialect plumbing (Postgres + Oracle),
schema bootstrap, the three baseline sources (bulk yfinance, single-symbol yfinance, daily_bhavcopy
bulk + per-connection), the batched upsert with its one-by-one fallback, the progress file and stop
flag the UI polls, the IST freshness check, the `precalculate_surprise_baselines` orchestrator and the
250-symbol fallback universe.

No network, no database, no real yfinance / pandas / requests. Every test loads a FRESH copy of the
module (so the job lock, stop flag and import-time env never leak between tests) with the progress file
pointed at a temp dir. `yfinance`, `pandas`, `requests`, `rate_limiter`, `symbol_aliases`,
`surprise_schema`, `zoneinfo` and `datetime` are small fakes in sys.modules; numpy and sqlalchemy's
`text` are the real ones. The DB is a fake engine that records every statement and bind. Clocks are
fake and nothing sleeps.

A final class guards the contracts main.py and surprise_scanner.py rely on.

Run from services/api-gateway:
    python3 -m pytest tests/test_surprise_premarket.py -v --cov --cov-report=term-missing
"""
from __future__ import annotations

import ast
import datetime as _real_dt
import importlib.util
import inspect
import json
import logging
import os
import re
import sys
import threading
import types
from types import SimpleNamespace

import numpy as np
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
_MOD_PATH = os.path.join(_SERVICE, "surprise_premarket.py")

_ENV_KEYS = (
    "SURPRISE_LOOKBACK_DAYS", "SURPRISE_LIQUID_MIN_TURNOVER", "SURPRISE_MAX_SYMBOLS",
    "SURPRISE_PREMARKET_WORKERS", "SURPRISE_UPSERT_BATCH", "SURPRISE_YF_BULK_BATCH",
    "SURPRISE_YF_BULK_PAUSE", "SURPRISE_PREMARKET_PROGRESS_PATH", "SURPRISE_UNIVERSE", "SCAN_UNIVERSE",
    "CACHE_DATABASE_URL", "DATABASE_URL", "TRAINING_DATABASE_URL", "ORACLE_DSN",
)


# ── fakes ─────────────────────────────────────────────────────────────────────

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


class Loader:
    def __init__(self, monkeypatch, tmp_path):
        self.mp = monkeypatch
        self.tmp = tmp_path
        self.n = 0

    def __call__(self, **env):
        for k in _ENV_KEYS:
            self.mp.delenv(k, raising=False)
        env.setdefault("SURPRISE_PREMARKET_PROGRESS_PATH", str(self.tmp / "progress.json"))
        for k, v in env.items():
            self.mp.setenv(k, v)
        self.n += 1
        spec = importlib.util.spec_from_file_location(f"surprise_premarket_under_test_{self.n}", _MOD_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


@pytest.fixture
def load(monkeypatch, tmp_path):
    return Loader(monkeypatch, tmp_path)


@pytest.fixture
def pm(load, monkeypatch):
    m = load()
    clock = Clock()
    monkeypatch.setattr(m, "time", clock)
    m._clock = clock
    return m


def schema_fake(**fns):
    m = types.ModuleType("surprise_schema")
    for k, v in fns.items():
        setattr(m, k, v)
    return m


def boom(*a, **k):
    raise RuntimeError("boom")


class Res:
    def __init__(self, rows=(), scalar=None, one=None):
        self.rows = list(rows)
        self.scalar_value = scalar
        self.one = one

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.one

    def scalar(self):
        return self.scalar_value


class FakeConn:
    def __init__(self, db):
        self.db = db
        self.dialect = SimpleNamespace(name=db.dialect_name)

    def execute(self, stmt, params=None):
        s = str(stmt)
        self.db.calls.append((s, params))
        if self.db.on_execute is not None:
            self.db.on_execute(s, params)
        if "information_schema" in s:
            return Res(one=(1,) if self.db.table_exists else None)
        if "user_tables" in s:
            return Res(scalar=1 if self.db.table_exists else 0)
        if self.db.batches:
            return Res(rows=self.db.batches.pop(0))
        return Res(rows=self.db.rows)


class _Ctx:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return FakeConn(self.db)

    def __exit__(self, *a):
        return False


class FakeDB:
    """Fake engine: begin()/connect() yield a conn recording SQL + binds; dispose() is counted."""

    def __init__(self, rows=(), dialect_name="postgresql", table_exists=True):
        self.rows = list(rows)
        self.batches = []          # optional: one result list per SELECT, consumed in order
        self.dialect_name = dialect_name
        self.table_exists = table_exists
        self.calls = []
        self.on_execute = None
        self.disposed = 0

    @property
    def sqls(self):
        return [c[0] for c in self.calls]

    def begin(self):
        return _Ctx(self)

    def connect(self):
        return _Ctx(self)

    def dispose(self):
        self.disposed += 1


@pytest.fixture
def dbpm(pm, monkeypatch):
    """Module wired to a FakeDB (Postgres by default). `_ss` is dropped so dispose() is observable."""
    db = FakeDB()
    monkeypatch.setattr(pm, "_ss", None)
    monkeypatch.setattr(pm, "_db_url", lambda: "postgresql://h/db")
    monkeypatch.setattr(pm, "_engine", lambda app_name, **kw: db)
    monkeypatch.setattr(pm, "_dialect", lambda: db.dialect_name)
    return pm, db


class FSeries:
    def __init__(self, vals):
        self.values = np.array(vals, dtype="float64")

    def astype(self, dt):
        return self


class FDF:
    """Single-ticker OHLCV frame over plain lists."""

    def __init__(self, cols):
        self.cols = {k: list(v) for k, v in cols.items()}
        self.columns = list(cols)
        self.n = len(next(iter(self.cols.values()))) if self.cols else 0

    @property
    def empty(self):
        return self.n == 0

    def dropna(self, how=None):
        return self

    def __len__(self):
        return self.n

    def tail(self, k):
        return FDF({c: v[-k:] for c, v in self.cols.items()})

    def __getitem__(self, key):
        return FSeries(self.cols[key])


class FakeMI:
    def __init__(self, names):
        self.levels = [list(names)]


class FMulti:
    """yfinance group_by='ticker' frame."""

    def __init__(self, frames):
        self.frames = frames
        self.columns = FakeMI(frames)
        self.empty = False

    def __getitem__(self, key):
        return self.frames[key]


def ohlc(n=10, close=100.0, high=110.0, low=90.0, vol=25000.0, closes=None, highs=None, lows=None, vols=None):
    return FDF({
        "Close": closes if closes is not None else [close] * n,
        "High": highs if highs is not None else [high] * n,
        "Low": lows if lows is not None else [low] * n,
        "Volume": vols if vols is not None else [vol] * n,
    })


class YF:
    """Fake yfinance + pandas + rate_limiter + symbol_aliases wired into sys.modules."""

    def __init__(self, monkeypatch):
        self.downloads = []
        self.results = []          # one result per yf.download call (frame | None | Exception)
        self.history = None        # what Ticker(...).history() returns (frame | None | Exception)
        self.tickers = []
        self.acquired = []
        self.delisted = set()
        self.renames = {}
        yf = types.ModuleType("yfinance")

        def download(tickers, **kw):
            self.downloads.append((tickers, kw))
            r = self.results.pop(0) if self.results else None
            if isinstance(r, Exception):
                raise r
            return r

        outer = self

        class Ticker:
            def __init__(self, sym):
                outer.tickers.append(sym)

            def history(self, **kw):
                if isinstance(outer.history, Exception):
                    raise outer.history
                return outer.history

        yf.download = download
        yf.Ticker = Ticker
        yf.shared = SimpleNamespace()
        yf.set_session = lambda sess: setattr(yf, "_sess", sess)
        self.yf = yf
        pd = types.ModuleType("pandas")
        pd.MultiIndex = FakeMI
        rl = types.ModuleType("rate_limiter")
        rl.acquire = lambda name, weight=1: self.acquired.append((name, weight))
        sa = types.ModuleType("symbol_aliases")
        sa.is_known_delisted = lambda s: s in self.delisted
        sa.resolve_base_symbol = lambda s: self.renames.get(s, s)
        rq = types.ModuleType("requests")

        class Session:
            def __init__(self):
                self.headers = {}

        class _H(dict):
            pass

        def _mk():
            s = Session()
            s.headers = _H()
            return s

        rq.Session = _mk
        for name, mod in (("yfinance", yf), ("pandas", pd), ("rate_limiter", rl),
                          ("symbol_aliases", sa), ("requests", rq)):
            monkeypatch.setitem(sys.modules, name, mod)


@pytest.fixture
def yf(monkeypatch):
    return YF(monkeypatch)


def row_for(sym, **over):
    r = {"symbol": sym, "prev_close": 100.0, "avg_15m_volume": 1000, "daily_atr": 20.0, "high_52w": 110.0,
         "dist_52w_pct": 9.09, "sector": None, "is_liquid": False}
    r.update(over)
    return r


# ── module constants / env ────────────────────────────────────────────────────

class TestConstants:
    def test_defaults(self, load):
        m = load()
        assert m.LOOKBACK_DAYS == 30
        assert m.LIQUID_MIN_DAILY_TURNOVER == 5_000_000.0
        assert m.MAX_SYMBOLS == 320
        assert m.MAX_WORKERS == 6
        assert m.UPSERT_BATCH == 40
        assert m.YF_BULK_BATCH_SIZE == 50
        assert m.YF_BULK_BATCH_PAUSE == 0.5
        assert m._ORACLE_IN_CHUNK == 900
        assert m._INDEX_SKIP >= {"NIFTY50", "NIFTY", "BANKNIFTY", "SENSEX"}
        assert m._job_lock is False
        assert m._PREMARKET_STOP_FLAG.is_set() is False

    def test_env_overrides(self, load, tmp_path):
        m = load(SURPRISE_LOOKBACK_DAYS="10", SURPRISE_LIQUID_MIN_TURNOVER="1000", SURPRISE_MAX_SYMBOLS="5",
                 SURPRISE_PREMARKET_WORKERS="2", SURPRISE_UPSERT_BATCH="3", SURPRISE_YF_BULK_BATCH="7",
                 SURPRISE_YF_BULK_PAUSE="0.1", SURPRISE_PREMARKET_PROGRESS_PATH=str(tmp_path / "x.json"))
        assert (m.LOOKBACK_DAYS, m.LIQUID_MIN_DAILY_TURNOVER, m.MAX_SYMBOLS) == (10, 1000.0, 5)
        assert (m.MAX_WORKERS, m.UPSERT_BATCH, m.YF_BULK_BATCH_SIZE, m.YF_BULK_BATCH_PAUSE) == (2, 3, 7, 0.1)
        assert m._PROGRESS_PATH == str(tmp_path / "x.json")

    def test_default_progress_path(self, load, monkeypatch):
        m = load()
        monkeypatch.delenv("SURPRISE_PREMARKET_PROGRESS_PATH")
        spec = importlib.util.spec_from_file_location("sp_default_path", _MOD_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod._PROGRESS_PATH == "/tmp/surprise_premarket_progress.json"
        assert m is not mod


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
    def test_cases(self, pm, url, want):
        assert pm._normalize_db_url(url) == want


# ── surprise_schema plumbing ──────────────────────────────────────────────────

class TestSchemaImport:
    def test_missing_schema_module_sets_none(self, load, monkeypatch):
        monkeypatch.setitem(sys.modules, "surprise_schema", None)
        assert load()._ss is None

    def test_schema_module_is_used_when_present(self, load, monkeypatch):
        fake = schema_fake()
        monkeypatch.setitem(sys.modules, "surprise_schema", fake)
        assert load()._ss is fake


class TestDialect:
    def test_from_schema(self, load, monkeypatch):
        monkeypatch.setitem(sys.modules, "surprise_schema", schema_fake(dialect=lambda: "oracle"))
        assert load()._dialect() == "oracle"

    def test_schema_error_falls_back_to_env(self, load, monkeypatch):
        monkeypatch.setitem(sys.modules, "surprise_schema", schema_fake(dialect=boom))
        m = load()
        assert m._dialect() == "postgresql"
        monkeypatch.setenv("ORACLE_DSN", "dsn")
        assert m._dialect() == "oracle"

    def test_no_schema_uses_env(self, load, monkeypatch):
        monkeypatch.setitem(sys.modules, "surprise_schema", None)
        m = load()
        assert m._dialect() == "postgresql"
        monkeypatch.setenv("ORACLE_DSN", "dsn")
        assert m._dialect() == "oracle"


class TestConnDialect:
    def test_reads_conn_dialect_lowercased(self, pm):
        assert pm._conn_dialect(SimpleNamespace(dialect=SimpleNamespace(name="ORACLE"))) == "oracle"

    def test_empty_name_gives_empty_string(self, pm):
        assert pm._conn_dialect(SimpleNamespace(dialect=SimpleNamespace(name=None))) == ""

    def test_broken_conn_falls_back_to_dialect(self, load, monkeypatch):
        monkeypatch.setitem(sys.modules, "surprise_schema", None)
        assert load()._conn_dialect(object()) == "postgresql"


class TestDbUrl:
    def test_prefers_schema_url(self, load, monkeypatch):
        monkeypatch.setitem(sys.modules, "surprise_schema", schema_fake(database_url=lambda: "postgresql://s/db"))
        assert load()._db_url() == "postgresql://s/db"

    def test_schema_error_falls_back_to_env(self, load, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "surprise_schema", schema_fake(database_url=boom))
        m = load(DATABASE_URL="postgres://h/db")
        with caplog.at_level(logging.DEBUG):
            assert m._db_url() == "postgresql://h/db?sslmode=require"
        assert "surprise_schema.database_url" in caplog.text

    @pytest.fixture
    def noschema(self, load, monkeypatch):
        monkeypatch.setitem(sys.modules, "surprise_schema", None)
        return load

    def test_env_precedence(self, noschema, monkeypatch):
        m = noschema(CACHE_DATABASE_URL="postgresql://c/x", DATABASE_URL="postgresql://d/x",
                     TRAINING_DATABASE_URL="postgresql://t/x")
        assert m._db_url().startswith("postgresql://c/x")
        monkeypatch.delenv("CACHE_DATABASE_URL")
        assert m._db_url().startswith("postgresql://d/x")
        monkeypatch.delenv("DATABASE_URL")
        assert m._db_url().startswith("postgresql://t/x")

    def test_none_when_unset(self, noschema):
        assert noschema()._db_url() is None

    def test_oracle_url_is_none(self, noschema):
        assert noschema(DATABASE_URL="ORACLE+oracledb://u:p@dsn")._db_url() is None


class TestEngine:
    def test_uses_shared_engine(self, load, monkeypatch):
        seen = []
        monkeypatch.setitem(sys.modules, "surprise_schema",
                            schema_fake(shared_engine=lambda app: seen.append(app) or "SHARED"))
        assert load()._engine("app-x", ignored=1) == "SHARED"
        assert seen == ["app-x"]

    def test_make_engine_when_no_shared_engine(self, load, monkeypatch):
        monkeypatch.setitem(sys.modules, "surprise_schema", schema_fake(make_engine=lambda app: ("MADE", app)))
        assert load()._engine("app-y") == ("MADE", "app-y")

    def test_schema_error_falls_back_to_create_engine(self, load, monkeypatch):
        import sqlalchemy
        monkeypatch.setattr(sqlalchemy, "create_engine", lambda url, **kw: SimpleNamespace(url=url, kw=kw))
        monkeypatch.setitem(sys.modules, "surprise_schema",
                            schema_fake(shared_engine=boom, database_url=lambda: "postgresql://h/db"))
        e = load()._engine("app-z")
        assert e.url == "postgresql://h/db"
        assert e.kw["pool_pre_ping"] is True
        assert (e.kw["pool_size"], e.kw["max_overflow"]) == (1, 1)
        assert e.kw["connect_args"] == {"connect_timeout": 20, "application_name": "app-z"}

    def test_no_url_gives_none(self, load, monkeypatch):
        monkeypatch.setitem(sys.modules, "surprise_schema", None)
        assert load()._engine("app") is None


class TestMaybeDispose:
    def test_shared_pool_is_never_disposed(self, pm, monkeypatch):
        monkeypatch.setattr(pm, "_ss", object())
        db = FakeDB()
        pm._maybe_dispose(db)
        assert db.disposed == 0

    def test_own_engine_is_disposed(self, pm, monkeypatch):
        monkeypatch.setattr(pm, "_ss", None)
        db = FakeDB()
        pm._maybe_dispose(db)
        assert db.disposed == 1

    def test_dispose_errors_are_swallowed(self, pm, monkeypatch):
        monkeypatch.setattr(pm, "_ss", None)
        pm._maybe_dispose(SimpleNamespace(dispose=boom))


class TestSymFilterAndChunks:
    def test_postgres_uses_any_array(self, pm):
        assert pm._sym_filter("symbol", "syms", "postgresql") == ("symbol = ANY(:syms)", None)

    def test_oracle_uses_expanding_in_list(self, pm):
        frag, bp = pm._sym_filter("symbol", "syms", "oracle")
        assert frag == "symbol IN :syms"
        assert bp.key == "syms" and bp.expanding is True

    def test_chunks(self, pm):
        assert pm._chunks(["a", "b", "c", "d", "e"], 2) == [["a", "b"], ["c", "d"], ["e"]]
        assert pm._chunks([], 3) == []


# ── ensure_schema ─────────────────────────────────────────────────────────────

class TestEnsureSchema:
    def test_schema_module_success(self, pm, monkeypatch):
        monkeypatch.setitem(sys.modules, "surprise_schema", schema_fake(ensure_surprise_schema=lambda: {"ok": True}))
        assert pm.ensure_schema() is True

    def test_schema_reports_not_ok_then_no_url(self, pm, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "surprise_schema",
                            schema_fake(ensure_surprise_schema=lambda: {"ok": False, "error": "nope"}))
        monkeypatch.setattr(pm, "_db_url", lambda: None)
        with caplog.at_level(logging.WARNING):
            assert pm.ensure_schema() is False
        assert "ensure_surprise_schema: nope" in caplog.text
        assert "cannot ensure surprise_static_feed" in caplog.text

    def test_oracle_without_schema_module_gives_up(self, pm, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "surprise_schema", None)
        monkeypatch.setattr(pm, "_db_url", lambda: "oracle://x")
        monkeypatch.setattr(pm, "_dialect", lambda: "oracle")
        with caplog.at_level(logging.ERROR):
            assert pm.ensure_schema() is False
        assert "Oracle mode but surprise_schema.py unavailable" in caplog.text

    def test_inline_ddl_on_postgres(self, pm, monkeypatch):
        import sqlalchemy
        db = FakeDB()
        created = []
        monkeypatch.setattr(sqlalchemy, "create_engine", lambda url, **kw: created.append((url, kw)) or db)
        monkeypatch.setitem(sys.modules, "surprise_schema", schema_fake(ensure_surprise_schema=boom))
        monkeypatch.setattr(pm, "_db_url", lambda: "postgresql://h/db")
        monkeypatch.setattr(pm, "_dialect", lambda: "postgresql")
        assert pm.ensure_schema() is True
        assert "CREATE TABLE IF NOT EXISTS surprise_static_feed" in db.sqls[0]
        assert "CREATE INDEX IF NOT EXISTS idx_surprise_static_sym" in db.sqls[1]
        assert db.disposed == 1
        assert created[0][1]["connect_args"]["application_name"] == "surprise-premarket-ddl"

    def test_inline_ddl_failure_returns_false(self, pm, monkeypatch, caplog):
        import sqlalchemy
        monkeypatch.setattr(sqlalchemy, "create_engine", boom)
        monkeypatch.setitem(sys.modules, "surprise_schema", None)
        monkeypatch.setattr(pm, "_db_url", lambda: "postgresql://h/db")
        monkeypatch.setattr(pm, "_dialect", lambda: "postgresql")
        with caplog.at_level(logging.ERROR):
            assert pm.ensure_schema() is False
        assert "schema ensure failed" in caplog.text


# ── _yahoo_sym / _yahoo_session ───────────────────────────────────────────────

class TestYahooSym:
    @pytest.mark.parametrize("raw,want", [
        ("tcs", "TCS.NS"), ("TCS.NS", "TCS.NS"), (" infy.bo ", "INFY.NS"), ("", ""), (None, ""), ("  ", ""),
        ("M&M", "M&M.NS"),
    ])
    def test_cases(self, pm, raw, want):
        assert pm._yahoo_sym(raw) == want


class TestYahooSession:
    def test_reuses_an_existing_session(self, pm, yf):
        yf.yf.shared._session = "EXISTING"
        assert pm._yahoo_session() == "EXISTING"

    def test_builds_and_installs_a_browser_like_session(self, pm, yf):
        sess = pm._yahoo_session()
        assert sess.headers["User-Agent"].startswith("Mozilla/5.0")
        assert sess.headers["Accept-Language"] == "en-US,en;q=0.9"
        assert yf.yf._sess is sess

    def test_falls_back_to_shared_attribute_without_set_session(self, pm, yf):
        del yf.yf.set_session
        sess = pm._yahoo_session()
        assert yf.yf.shared._session is sess

    def test_missing_libraries_give_none(self, pm, yf, monkeypatch):
        monkeypatch.setitem(sys.modules, "yfinance", None)
        assert pm._yahoo_session() is None


# ── bulk_baselines_from_yfinance ──────────────────────────────────────────────

@pytest.fixture
def bulk(pm, yf, monkeypatch):
    monkeypatch.setattr(pm, "_yahoo_session", lambda: None)
    return pm, yf


class TestBulkYfinance:
    def test_missing_libraries(self, pm, yf, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "pandas", None)
        with caplog.at_level(logging.ERROR):
            assert pm.bulk_baselines_from_yfinance(["A", "B"]) == ([], ["A", "B"])
        assert "numpy/pandas/yfinance required" in caplog.text

    def test_multi_ticker_extraction(self, bulk):
        pm, yf = bulk
        yf.results = [FMulti({
            "AAA.NS": ohlc(closes=[100.0] * 9 + [105.0], vols=[100000.0] * 10),
            "BBB.NS": ohlc(),
        })]
        rows, remaining = pm.bulk_baselines_from_yfinance(["AAA", "BBB"])
        a, b = rows
        assert a == {"symbol": "AAA", "prev_close": 105.0, "avg_15m_volume": 4000, "daily_atr": 20.0,
                     "high_52w": 110.0, "dist_52w_pct": 4.55, "sector": None, "is_liquid": True}
        assert b["is_liquid"] is False and b["avg_15m_volume"] == 1000 and b["dist_52w_pct"] == 9.09
        assert remaining == []
        tickers, kw = yf.downloads[0]
        assert tickers == "AAA.NS BBB.NS"
        assert kw == {"period": "1y", "interval": "1d", "group_by": "ticker", "threads": True,
                      "progress": False, "auto_adjust": True}

    def test_lookback_window_limits_the_averages(self, bulk):
        pm, yf = bulk
        # 60 bars: the first 30 are huge, the last 30 (LOOKBACK_DAYS) are small -> only the tail counts
        yf.results = [FMulti({"AAA.NS": ohlc(closes=[100.0] * 60, highs=[500.0] * 30 + [110.0] * 30,
                                             lows=[90.0] * 60, vols=[1e9] * 30 + [25000.0] * 30)})]
        rows, _ = pm.bulk_baselines_from_yfinance(["AAA"])
        assert rows[0]["avg_15m_volume"] == 1000 and rows[0]["daily_atr"] == 20.0
        assert rows[0]["high_52w"] == 500.0          # the 52W high uses the whole frame

    def test_single_ticker_frame(self, bulk):
        pm, yf = bulk
        yf.results = [ohlc()]
        rows, remaining = pm.bulk_baselines_from_yfinance(["AAA"])
        assert [r["symbol"] for r in rows] == ["AAA"] and remaining == []

    def test_rate_limiter_is_paced_per_batch(self, bulk):
        pm, yf = bulk
        yf.results = [None, None]
        pm.bulk_baselines_from_yfinance(["A", "B", "C"], batch_size=2)
        assert yf.acquired == [("yfinance", 2), ("yfinance", 1)]

    def test_rate_limiter_missing_is_ignored(self, bulk, monkeypatch):
        pm, yf = bulk
        monkeypatch.setitem(sys.modules, "rate_limiter", None)
        yf.results = [ohlc()]
        assert len(pm.bulk_baselines_from_yfinance(["AAA"])[0]) == 1

    def test_symbol_cleaning(self, bulk):
        pm, yf = bulk
        yf.delisted = {"DEAD"}
        yf.renames = {"JUBILANT": "JUBLFOOD", "AAPL": None}
        yf.results = [None]
        rows, remaining = pm.bulk_baselines_from_yfinance(
            [" tcs.ns ", "", None, "NIFTY50", "^NSEI", "DEAD", "AAPL", "JUBILANT", "INFY.BO"])
        assert yf.downloads[0][0] == "TCS.NS JUBLFOOD.NS INFY.NS"
        assert remaining == ["TCS", "JUBLFOOD", "INFY"]

    def test_alias_module_missing_leaves_symbols_alone(self, bulk, monkeypatch):
        pm, yf = bulk
        monkeypatch.setitem(sys.modules, "symbol_aliases", None)
        yf.results = [None]
        pm.bulk_baselines_from_yfinance(["JUBILANT"])
        assert yf.downloads[0][0] == "JUBILANT.NS"

    def test_batches_with_a_pause_between_them_only(self, bulk):
        pm, yf = bulk
        yf.results = [ohlc(), ohlc(), ohlc()]
        pm.bulk_baselines_from_yfinance([f"S{i}" for i in range(5)], batch_size=2)
        assert [len(t.split()) for t, _ in yf.downloads] == [2, 2, 1]
        assert pm._clock.sleeps == [pm.YF_BULK_BATCH_PAUSE, pm.YF_BULK_BATCH_PAUSE]

    def test_pause_before_every_batch_after_the_first_even_when_earlier_ones_fail(self, bulk):
        # A failed download and an empty/None frame used to `continue` past the pacing sleep, so exactly the
        # batches that most likely hit a rate limit got no pause. The pause now sits before each batch.
        pm, yf = bulk
        yf.results = [RuntimeError("429"), None, ohlc()]
        pm.bulk_baselines_from_yfinance([f"S{i}" for i in range(5)], batch_size=2)
        assert len(yf.downloads) == 3
        assert pm._clock.sleeps == [pm.YF_BULK_BATCH_PAUSE, pm.YF_BULK_BATCH_PAUSE]

    def test_pause_lands_before_the_next_download_not_after_the_last(self, bulk):
        pm, yf = bulk
        order = []
        real_sleep = pm._clock.sleep
        pm._clock.sleep = lambda s: (order.append("sleep"), real_sleep(s))[1]
        orig = yf.yf.download
        yf.yf.download = lambda tickers, **kw: (order.append("download"), orig(tickers, **kw))[1]
        yf.results = [RuntimeError("429"), RuntimeError("429")]
        pm.bulk_baselines_from_yfinance(["A", "B"], batch_size=1)
        assert order == ["download", "sleep", "download"]

    def test_single_batch_never_pauses(self, bulk):
        pm, yf = bulk
        yf.results = [RuntimeError("429")]
        pm.bulk_baselines_from_yfinance(["A", "B"], batch_size=50)
        assert pm._clock.sleeps == []

    def test_download_failure_skips_the_batch(self, bulk, caplog):
        pm, yf = bulk
        yf.results = [RuntimeError("429"), FMulti({"BBB.NS": ohlc()})]
        with caplog.at_level(logging.WARNING):
            rows, remaining = pm.bulk_baselines_from_yfinance(["AAA", "BBB"], batch_size=1)
        assert [r["symbol"] for r in rows] == ["BBB"] and remaining == ["AAA"]
        assert "batch [0:1] failed" in caplog.text

    def test_none_and_empty_frames_are_skipped(self, bulk):
        pm, yf = bulk
        yf.results = [None, ohlc(n=0)]
        rows, remaining = pm.bulk_baselines_from_yfinance(["AAA", "BBB"], batch_size=1)
        assert rows == [] and remaining == ["AAA", "BBB"]

    def test_object_without_pandas_api_is_survivable(self, bulk):
        pm, yf = bulk
        yf.results = [object()]
        rows, remaining = pm.bulk_baselines_from_yfinance(["AAA"])
        assert rows == [] and remaining == ["AAA"]

    def test_frame_whose_dropna_gives_none(self, bulk):
        pm, yf = bulk
        yf.results = [SimpleNamespace(empty=False, dropna=lambda how=None: None)]
        assert pm.bulk_baselines_from_yfinance(["AAA"]) == ([], ["AAA"])

    def test_missing_ticker_column_and_short_history(self, bulk):
        pm, yf = bulk
        yf.results = [FMulti({"AAA.NS": ohlc(n=4)})]
        rows, remaining = pm.bulk_baselines_from_yfinance(["AAA", "BBB"])
        assert rows == [] and remaining == ["AAA", "BBB"]

    @pytest.mark.parametrize("frame", [
        ohlc(highs=[0.0] * 10, lows=[0.0] * 10),        # 52W high <= 0
        ohlc(closes=[0.0] * 10),                        # prev close <= 0
        FDF({"Close": [1.0] * 10}),                     # missing columns -> extraction error, swallowed
    ])
    def test_unusable_frames_stay_remaining(self, bulk, frame):
        pm, yf = bulk
        yf.results = [frame]
        assert pm.bulk_baselines_from_yfinance(["AAA"]) == ([], ["AAA"])

    def test_zero_volume_still_gives_a_floor_of_one(self, bulk):
        pm, yf = bulk
        yf.results = [ohlc(vol=0.0)]
        assert pm.bulk_baselines_from_yfinance(["AAA"])[0][0]["avg_15m_volume"] == 1

    def test_turnover_threshold_is_env_driven(self, load, monkeypatch, yf):
        m = load(SURPRISE_LIQUID_MIN_TURNOVER="2500000")
        monkeypatch.setattr(m, "time", Clock())
        monkeypatch.setattr(m, "_yahoo_session", lambda: None)
        yf.results = [ohlc(vol=25000.0)]                 # turnover exactly 2.5M -> liquid (>=)
        assert m.bulk_baselines_from_yfinance(["AAA"])[0][0]["is_liquid"] is True


# ── compute_baseline_for_symbol ───────────────────────────────────────────────

class TestSingleSymbolBaseline:
    @pytest.fixture(autouse=True)
    def _session(self, pm, monkeypatch):
        monkeypatch.setattr(pm, "_yahoo_session", lambda: None)

    def test_missing_libraries(self, pm, yf, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "yfinance", None)
        with caplog.at_level(logging.ERROR):
            assert pm.compute_baseline_for_symbol("AAA") is None
        assert "numpy/yfinance required" in caplog.text

    @pytest.mark.parametrize("sym", ["NIFTY50", "^NSEI", "", "  .NS"])
    def test_index_and_blank_symbols(self, pm, yf, sym):
        assert pm.compute_baseline_for_symbol(sym) is None
        assert yf.tickers == []

    def test_builds_a_baseline(self, pm, yf):
        yf.history = ohlc(closes=[100.0] * 9 + [95.0], vols=[100000.0] * 10)
        row = pm.compute_baseline_for_symbol(" aaa.ns ")
        assert row == {"symbol": "AAA", "prev_close": 95.0, "avg_15m_volume": 4000, "daily_atr": 20.0,
                       "high_52w": 110.0, "dist_52w_pct": 13.64, "sector": None, "is_liquid": True}
        assert yf.tickers == ["AAA.NS"]
        assert yf.acquired == [("yfinance", 1)]

    def test_rate_limiter_failure_is_ignored(self, pm, yf, monkeypatch):
        monkeypatch.setitem(sys.modules, "rate_limiter", None)
        yf.history = ohlc()
        assert pm.compute_baseline_for_symbol("AAA")["symbol"] == "AAA"

    @pytest.mark.parametrize("hist", [None, ohlc(n=0), ohlc(n=4)])
    def test_no_or_short_history(self, pm, yf, hist):
        yf.history = hist
        assert pm.compute_baseline_for_symbol("AAA") is None

    def test_non_positive_52w_high(self, pm, yf):
        yf.history = ohlc(highs=[0.0] * 10, lows=[0.0] * 10)
        assert pm.compute_baseline_for_symbol("AAA") is None

    def test_history_error(self, pm, yf, caplog):
        yf.history = RuntimeError("delisted")
        with caplog.at_level(logging.DEBUG):
            assert pm.compute_baseline_for_symbol("AAA") is None
        assert "baseline AAA failed" in caplog.text


# ── compute_baseline_from_bhavcopy / _table_exists ────────────────────────────

def bhav_rows(n=6, close=100.0, high=110.0, low=90.0, vol=25000.0, symbol=None):
    out = []
    for _ in range(n):
        r = {"high": high, "low": low, "close": close, "volume": vol}
        if symbol is not None:
            r["symbol"] = symbol
        out.append(r)
    return out


def install_clock(monkeypatch, y=2026, mo=9, d=30, h=11, mi=0):
    """Fake `zoneinfo` + `datetime` so 'now in IST' is exactly the given wall-clock time."""
    zmod = types.ModuleType("zoneinfo")
    zmod.ZoneInfo = lambda name: _real_dt.timezone(_real_dt.timedelta(hours=5, minutes=30))
    dmod = types.ModuleType("datetime")
    # Start from the complete real module so a first-time `import sqlalchemy` (or anything else that
    # reads datetime.tzinfo / MINYEAR ...) under this fake still works when a test runs in isolation.
    dmod.__dict__.update({k: v for k, v in vars(_real_dt).items() if not k.startswith("__")})

    class FakeDT(_real_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _real_dt.datetime(y, mo, d, h, mi, tzinfo=tz)

    dmod.datetime = FakeDT
    monkeypatch.setitem(sys.modules, "zoneinfo", zmod)
    monkeypatch.setitem(sys.modules, "datetime", dmod)


class TestBaselineFromBhavcopy:
    def _conn(self, rows, dialect="postgresql"):
        db = FakeDB(rows=rows, dialect_name=dialect)
        return FakeConn(db), db

    def test_builds_a_baseline_newest_row_first(self, pm):
        rows = bhav_rows(6)
        rows[0]["close"] = 105.0                               # rows[0] is the most recent session
        conn, db = self._conn(rows)
        assert pm.compute_baseline_from_bhavcopy(" aaa.ns ", conn) == {
            "symbol": "AAA", "prev_close": 105.0, "avg_15m_volume": 1000, "daily_atr": 20.0,
            "high_52w": 110.0, "dist_52w_pct": 4.55, "sector": None, "is_liquid": False}
        sql, params = db.calls[0]
        assert "FROM daily_bhavcopy" in sql and "LIMIT 20" in sql and "FETCH FIRST" not in sql
        assert params == {"sym": "AAA"}

    def test_oracle_uses_fetch_first(self, pm):
        conn, db = self._conn(bhav_rows(6), "oracle")
        pm.compute_baseline_from_bhavcopy("AAA", conn)
        assert "FETCH FIRST 20 ROWS ONLY" in db.sqls[0] and "LIMIT" not in db.sqls[0]

    def test_liquidity_flag_uses_turnover(self, pm):
        conn, _ = self._conn(bhav_rows(6, vol=100000.0))         # 1e5 * 100 = 1e7 >= 5e6
        assert pm.compute_baseline_from_bhavcopy("AAA", conn)["is_liquid"] is True

    @pytest.mark.parametrize("n", [0, 4])
    def test_too_few_rows(self, pm, n):
        conn, _ = self._conn(bhav_rows(n))
        assert pm.compute_baseline_from_bhavcopy("AAA", conn) is None

    def test_exactly_five_rows_is_enough(self, pm):
        conn, _ = self._conn(bhav_rows(5))
        assert pm.compute_baseline_from_bhavcopy("AAA", conn)["symbol"] == "AAA"

    def test_non_positive_high_is_rejected(self, pm):
        conn, _ = self._conn(bhav_rows(6, high=0.0, low=0.0))
        assert pm.compute_baseline_from_bhavcopy("AAA", conn) is None

    def test_null_volume_counts_as_zero_with_a_floor_of_one(self, pm):
        conn, _ = self._conn(bhav_rows(6, vol=None))
        assert pm.compute_baseline_from_bhavcopy("AAA", conn)["avg_15m_volume"] == 1

    def test_query_error_falls_back_silently(self, pm, caplog):
        conn, db = self._conn(bhav_rows(6))
        db.on_execute = lambda s, p: boom()
        with caplog.at_level(logging.DEBUG):
            assert pm.compute_baseline_from_bhavcopy("AAA", conn) is None
        assert "bhavcopy path AAA" in caplog.text

    def test_unparseable_value_falls_back_silently(self, pm):
        conn, _ = self._conn(bhav_rows(6, close="abc"))
        assert pm.compute_baseline_from_bhavcopy("AAA", conn) is None

    def test_sqlalchemy_missing_falls_back_silently(self, pm, monkeypatch):
        monkeypatch.setitem(sys.modules, "sqlalchemy", None)
        conn, _ = self._conn(bhav_rows(6))
        assert pm.compute_baseline_from_bhavcopy("AAA", conn) is None


class TestTableExists:
    def test_postgres_true_and_false(self, pm):
        db = FakeDB(table_exists=True)
        assert pm._table_exists(FakeConn(db), "daily_bhavcopy") is True
        assert "information_schema.tables" in db.sqls[0]
        assert db.calls[0][1] == {"t": "daily_bhavcopy"}
        assert pm._table_exists(FakeConn(FakeDB(table_exists=False)), "x") is False

    def test_oracle_counts_user_tables(self, pm):
        db = FakeDB(dialect_name="oracle", table_exists=True)
        assert pm._table_exists(FakeConn(db), "daily_bhavcopy") is True
        assert "user_tables" in db.sqls[0] and "UPPER(:t)" in db.sqls[0]
        assert pm._table_exists(FakeConn(FakeDB(dialect_name="oracle", table_exists=False)), "x") is False

    def test_errors_mean_no_table(self, pm):
        db = FakeDB()
        db.on_execute = lambda s, p: boom()
        assert pm._table_exists(FakeConn(db), "x") is False

    def test_sqlalchemy_missing_means_no_table(self, pm, monkeypatch):
        monkeypatch.setitem(sys.modules, "sqlalchemy", None)
        assert pm._table_exists(FakeConn(FakeDB()), "x") is False


# ── bulk_baselines_from_bhavcopy ──────────────────────────────────────────────

# An expanding bindparam prints as ":syms" with the stub-free text() and as "(__[POSTCOMPILE_syms])" once
# SQLAlchemy has processed it; either way it must be an `IN` list over the `syms` bind.
_IN_LIST = re.compile(r"symbol IN \(?(?::syms|__\[POSTCOMPILE_syms\])")


def bulk_raw(sym, n=6, **kw):
    return bhav_rows(n, symbol=sym, **kw)


class TestBulkBhavcopy:
    def test_no_url_or_no_symbols(self, pm, monkeypatch):
        monkeypatch.setattr(pm, "_db_url", lambda: None)
        assert pm.bulk_baselines_from_bhavcopy(["A"]) == ([], ["A"])
        monkeypatch.setattr(pm, "_db_url", lambda: "postgresql://h/db")
        assert pm.bulk_baselines_from_bhavcopy([]) == ([], [])

    def test_engine_none(self, dbpm, monkeypatch):
        pm, db = dbpm
        monkeypatch.setattr(pm, "_engine", lambda app_name, **kw: None)
        assert pm.bulk_baselines_from_bhavcopy(["A"]) == ([], ["A"])

    def test_missing_table(self, dbpm):
        pm, db = dbpm
        db.table_exists = False
        assert pm.bulk_baselines_from_bhavcopy(["A", "B"]) == ([], ["A", "B"])
        assert db.disposed == 1

    def test_all_blank_symbols(self, dbpm):
        pm, db = dbpm
        assert pm.bulk_baselines_from_bhavcopy([" ", ".NS"]) == ([], [" ", ".NS"])
        assert db.disposed == 1

    def test_builds_rows_and_reports_the_remainder(self, dbpm):
        pm, db = dbpm
        db.rows = (bulk_raw("AAA", close=100.0, vol=100000.0) + bulk_raw("SHORT", n=4) +
                   bulk_raw("ZERO", high=0.0, low=0.0) + bulk_raw("BADNUM", close="abc"))
        rows, remaining = pm.bulk_baselines_from_bhavcopy([" aaa.ns", "SHORT", "ZERO", "BADNUM", "MISSING.BO"])
        assert rows == [{"symbol": "AAA", "prev_close": 100.0, "avg_15m_volume": 4000, "daily_atr": 20.0,
                         "high_52w": 110.0, "dist_52w_pct": 9.09, "sector": None, "is_liquid": True}]
        assert remaining == ["SHORT", "ZERO", "BADNUM", "MISSING"]
        assert db.disposed == 1

    def test_postgres_runs_one_query_with_an_array_bind(self, dbpm):
        pm, db = dbpm
        pm.bulk_baselines_from_bhavcopy(["aaa", "BBB.NS"])
        sqls = [c for c in db.calls if "ROW_NUMBER()" in c[0]]
        assert len(sqls) == 1
        assert "symbol = ANY(:syms)" in sqls[0][0] and "rn <= 20" in sqls[0][0]
        assert sqls[0][1] == {"syms": ["AAA", "BBB"]}

    def test_oracle_chunks_the_in_list(self, dbpm):
        pm, db = dbpm
        db.dialect_name = "oracle"
        db.batches = [bulk_raw("S0"), bulk_raw("S950")]
        syms = [f"S{i}" for i in range(1000)]
        rows, remaining = pm.bulk_baselines_from_bhavcopy(syms)
        q = [c for c in db.calls if "ROW_NUMBER()" in c[0]]
        assert [len(c[1]["syms"]) for c in q] == [900, 100]
        assert _IN_LIST.search(q[0][0])
        assert [r["symbol"] for r in rows] == ["S0", "S950"] and len(remaining) == 998

    def test_query_failure_gives_everything_back(self, dbpm, caplog):
        pm, db = dbpm
        db.on_execute = lambda s, p: boom() if "ROW_NUMBER" in s else None
        with caplog.at_level(logging.DEBUG):
            assert pm.bulk_baselines_from_bhavcopy(["A", "B"]) == ([], ["A", "B"])
        assert "bulk bhavcopy query failed" in caplog.text
        assert db.disposed == 1

    def test_outer_failure_gives_everything_back(self, dbpm, monkeypatch, caplog):
        pm, db = dbpm
        monkeypatch.setitem(sys.modules, "sqlalchemy", None)
        with caplog.at_level(logging.DEBUG):
            assert pm.bulk_baselines_from_bhavcopy(["A"]) == ([], ["A"])
        assert "bulk_baselines_from_bhavcopy" in caplog.text


# ── upsert_baselines ──────────────────────────────────────────────────────────

class TestUpsertBaselines:
    def test_empty_rows_and_no_url(self, pm, monkeypatch):
        assert pm.upsert_baselines([]) == 0
        monkeypatch.setattr(pm, "_db_url", lambda: None)
        assert pm.upsert_baselines([row_for("A")]) == 0

    def test_engine_none(self, dbpm, monkeypatch):
        pm, db = dbpm
        monkeypatch.setattr(pm, "_engine", lambda app_name, **kw: None)
        assert pm.upsert_baselines([row_for("A")]) == 0

    def test_inline_postgres_statement_when_no_schema_module(self, dbpm):
        pm, db = dbpm
        rows = [row_for("A"), row_for("B")]
        assert pm.upsert_baselines(rows) == 2
        assert len(db.calls) == 1                              # ONE executemany round-trip
        assert "ON CONFLICT (symbol) DO UPDATE" in db.calls[0][0]
        assert db.calls[0][1] == rows
        assert db.disposed == 1

    def test_schema_module_supplies_sql_and_adapts_rows(self, dbpm, monkeypatch):
        pm, db = dbpm
        seen = {}

        def upsert_sql(dial):
            seen["dial"] = dial
            return "MERGE INTO x"

        def adapt_rows(rows, dial):
            return [dict(r, is_liquid=1) for r in rows]

        monkeypatch.setattr(pm, "_ss", schema_fake(upsert_sql=upsert_sql, adapt_rows=adapt_rows))
        assert pm.upsert_baselines([row_for("A")]) == 1
        assert seen["dial"] == "postgresql"
        assert db.calls[0][0] == "MERGE INTO x"
        assert db.calls[0][1][0]["is_liquid"] == 1
        assert db.disposed == 0                                # the shared pool is never disposed

    def test_batch_failure_falls_back_to_one_by_one(self, dbpm, caplog):
        pm, db = dbpm

        def on_execute(sql, params):
            if isinstance(params, list) or (isinstance(params, dict) and params["symbol"] == "BAD"):
                raise RuntimeError("bad row")

        db.on_execute = on_execute
        with caplog.at_level(logging.ERROR):
            assert pm.upsert_baselines([row_for("A"), row_for("BAD"), row_for("C")]) == 2
        assert "upsert_baselines failed (3 rows)" in caplog.text
        assert [c[1]["symbol"] for c in db.calls[1:]] == ["A", "BAD", "C"]
        assert db.disposed == 1

    def test_fallback_engine_none(self, dbpm, monkeypatch):
        pm, db = dbpm
        engines = iter([db, None])
        monkeypatch.setattr(pm, "_engine", lambda app_name, **kw: next(engines))
        db.on_execute = lambda s, p: boom()
        assert pm.upsert_baselines([row_for("A")]) == 0

    def test_fallback_failure_is_logged(self, dbpm, monkeypatch, caplog):
        pm, db = dbpm
        state = {"n": 0}

        def engine(app_name, **kw):
            state["n"] += 1
            if state["n"] == 2:
                raise RuntimeError("no engine")
            return db

        monkeypatch.setattr(pm, "_engine", engine)
        db.on_execute = lambda s, p: boom()
        with caplog.at_level(logging.ERROR):
            assert pm.upsert_baselines([row_for("A")]) == 0
        assert "upsert fallback failed" in caplog.text


# ── stop flag + progress file ─────────────────────────────────────────────────

class TestStopFlag:
    def test_request_and_clear(self, pm):
        assert pm.premarket_stop_requested() is False
        pm.request_premarket_stop()
        assert pm.premarket_stop_requested() is True
        pm.clear_premarket_stop()
        assert pm.premarket_stop_requested() is False

    def test_flag_is_a_threading_event(self, pm):
        assert isinstance(pm._PREMARKET_STOP_FLAG, threading.Event)


IDLE = {"stage": "idle", "percent": 0, "processed": 0, "total": 0, "errors": 0, "elapsed_sec": 0,
        "eta_sec": None, "is_running": False, "current_symbol": None, "message": "Idle"}


class TestProgressFile:
    def test_round_trip_stamps_updated_at(self, pm):
        pm._write_progress({"stage": "computing", "percent": 40})
        got = pm.get_premarket_progress()
        assert got == {"stage": "computing", "percent": 40, "updated_at": pm._clock.now}

    def test_input_dict_is_not_mutated(self, pm):
        data = {"stage": "x"}
        pm._write_progress(data)
        assert data == {"stage": "x"}

    def test_missing_file_reads_as_idle(self, pm):
        assert pm.get_premarket_progress() == IDLE

    @pytest.mark.parametrize("content", ["[1, 2]", "{not json", '"str"', ""])
    def test_unusable_file_reads_as_idle(self, pm, content):
        with open(pm._PROGRESS_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        assert pm.get_premarket_progress() == IDLE

    def test_write_failure_is_swallowed(self, load, tmp_path, caplog):
        m = load(SURPRISE_PREMARKET_PROGRESS_PATH=str(tmp_path / "no_such_dir" / "p.json"))
        with caplog.at_level(logging.DEBUG):
            m._write_progress({"stage": "x"})
        assert "progress write" in caplog.text
        assert m.get_premarket_progress() == IDLE


# ── _freshness_check ──────────────────────────────────────────────────────────

class TestFreshnessCheck:
    @pytest.fixture(autouse=True)
    def _ist_clock(self, monkeypatch):
        install_clock(monkeypatch)                            # Wed 2026-09-30 11:00 IST

    def test_no_url_or_no_symbols(self, pm, monkeypatch):
        monkeypatch.setattr(pm, "_db_url", lambda: None)
        assert pm._freshness_check(["A", "B"]) == {"fresh": 0, "total": 2, "coverage": 0.0}
        monkeypatch.setattr(pm, "_db_url", lambda: "postgresql://h/db")
        assert pm._freshness_check([]) == {"fresh": 0, "total": 0, "coverage": 0.0}

    def test_engine_none(self, dbpm, monkeypatch):
        pm, _ = dbpm
        monkeypatch.setattr(pm, "_engine", lambda app_name, **kw: None)
        assert pm._freshness_check(["A"]) == {"fresh": 0, "total": 1, "coverage": 0.0}

    def test_missing_table(self, dbpm):
        pm, db = dbpm
        db.table_exists = False
        assert pm._freshness_check(["a.ns", "B", " "]) == {"fresh": 0, "total": 2, "coverage": 0.0}
        assert db.disposed == 1

    def test_postgres_query_shape(self, dbpm):
        pm, db = dbpm
        pm._freshness_check(["aaa.ns", "BBB"])
        q = [c for c in db.calls if "FROM surprise_static_feed" in c[0]]
        assert len(q) == 1 and "symbol = ANY(:syms)" in q[0][0]
        assert q[0][1] == {"syms": ["AAA", "BBB"]}

    def test_counts_rows_updated_today_in_ist(self, dbpm):
        pm, db = dbpm
        utc = _real_dt.timezone.utc
        db.rows = [
            ("A", _real_dt.datetime(2026, 9, 30, 5, 0, tzinfo=utc)),     # 10:30 IST, today
            ("B", _real_dt.datetime(2026, 9, 29, 20, 0, tzinfo=utc)),    # 01:30 IST on the 30th -> today
            ("C", _real_dt.datetime(2026, 9, 29, 10, 0, tzinfo=utc)),    # 15:30 IST on the 29th -> stale
            ("D", _real_dt.datetime(2026, 9, 30, 3, 0)),                 # naive on Postgres: taken as-is
            ("E", None),                                                 # unreadable -> stale
        ]
        assert pm._freshness_check(list("ABCDE")) == {"fresh": 3, "total": 5, "coverage": 0.6}

    def test_naive_oracle_timestamps_are_read_as_utc(self, dbpm):
        pm, db = dbpm
        db.dialect_name = "oracle"
        # naive 2026-09-29 20:00 is UTC on Oracle ADB -> 01:30 IST on the 30th -> fresh
        db.rows = [("A", _real_dt.datetime(2026, 9, 29, 20, 0)), ("B", _real_dt.datetime(2026, 9, 29, 10, 0))]
        assert pm._freshness_check(["A", "B"]) == {"fresh": 1, "total": 2, "coverage": 0.5}

    def test_oracle_chunks_the_in_list(self, dbpm):
        pm, db = dbpm
        db.dialect_name = "oracle"
        db.batches = [[("S0", _real_dt.datetime(2026, 9, 30, 0, 0))], []]
        out = pm._freshness_check([f"S{i}" for i in range(1000)])
        q = [c for c in db.calls if "FROM surprise_static_feed" in c[0]]
        assert [len(c[1]["syms"]) for c in q] == [900, 100]
        assert _IN_LIST.search(q[0][0])
        assert out == {"fresh": 1, "total": 1000, "coverage": 0.001}

    def test_blank_only_symbols_use_a_denominator_of_one(self, dbpm):
        pm, _ = dbpm
        assert pm._freshness_check([" "]) == {"fresh": 0, "total": 1, "coverage": 0.0}

    def test_failure_reports_nothing_fresh(self, dbpm, monkeypatch, caplog):
        pm, db = dbpm
        monkeypatch.setitem(sys.modules, "sqlalchemy", None)
        with caplog.at_level(logging.DEBUG):
            assert pm._freshness_check(["A", "B"]) == {"fresh": 0, "total": 2, "coverage": 0.0}
        assert "freshness check failed" in caplog.text


# ── precalculate_surprise_baselines ───────────────────────────────────────────

class PreRig:
    """precalculate_surprise_baselines() with every collaborator replaced by a recording stub.

    Progress writes are captured in a list (nothing touches the progress file), upserts record the
    symbols of each batch, and the three baseline sources are plain lambdas the test can swap out.
    """

    def __init__(self, pm, monkeypatch):
        self.pm = pm
        self.mp = monkeypatch
        self.clock = pm._clock
        self.progress = []
        self.upserts = []
        self.freshness_calls = []
        self.bhav_calls = []
        self.yf_calls = []
        self.compute_calls = []
        self.schema_ok = True
        self.fresh = lambda syms: {"fresh": 0, "total": len(syms), "coverage": 0.0}
        self.bhav = lambda syms: ([], list(syms))
        self.yfbulk = lambda syms: ([], list(syms))
        self.compute = {}                     # symbol -> row | None | Exception (default: a row)
        monkeypatch.setattr(pm, "ensure_schema", lambda: self.schema_ok)
        monkeypatch.setattr(pm, "_freshness_check", self._fresh)
        monkeypatch.setattr(pm, "bulk_baselines_from_bhavcopy", self._bhav)
        monkeypatch.setattr(pm, "bulk_baselines_from_yfinance", self._yf)
        monkeypatch.setattr(pm, "compute_baseline_for_symbol", self._compute)
        monkeypatch.setattr(pm, "upsert_baselines", self._upsert)
        monkeypatch.setattr(pm, "_write_progress", lambda d: self.progress.append(dict(d)))

    def _fresh(self, syms):
        self.freshness_calls.append(list(syms))
        return self.fresh(syms)

    def _bhav(self, syms):
        self.bhav_calls.append(list(syms))
        return self.bhav(syms)

    def _yf(self, syms):
        self.yf_calls.append(list(syms))
        return self.yfbulk(syms)

    def _compute(self, sym):
        self.compute_calls.append(sym)
        r = self.compute.get(sym, row_for(sym))
        if isinstance(r, Exception):
            raise r
        return r

    def _upsert(self, rows):
        self.upserts.append([r["symbol"] for r in rows])
        return len(rows)

    def run(self, symbols, **kw):
        return self.pm.precalculate_surprise_baselines(symbols, **kw)

    @property
    def stages(self):
        return [p["stage"] for p in self.progress]

    @property
    def last(self):
        return self.progress[-1]


@pytest.fixture
def mkrig(load, monkeypatch):
    def make(**env):
        m = load(**env)
        clock = Clock()
        monkeypatch.setattr(m, "time", clock)
        m._clock = clock
        return PreRig(m, monkeypatch)
    return make


@pytest.fixture
def pre(mkrig):
    return mkrig()


def rows_for(*syms):
    return [row_for(s) for s in syms]


class TestPrecalcGuards:
    def test_second_call_while_running_is_refused_untouched(self, pre):
        pre.pm._job_lock = True
        out = pre.run(["A"])
        assert out["ok"] is False and out["error"] == "already_running"
        assert out["progress"] == IDLE
        assert pre.progress == [] and pre.bhav_calls == []
        assert pre.pm._job_lock is True                # the refused call must not free the running job's lock

    def test_schema_failure(self, pre):
        pre.schema_ok = False
        assert pre.run(["A"]) == {"ok": False, "error": "schema_failed", "upserted": 0}
        assert pre.last["stage"] == "error" and pre.last["error"] == "schema_failed"
        assert pre.last["is_running"] is False
        assert pre.bhav_calls == [] and pre.pm._job_lock is False

    def test_unexpected_error_is_reported_and_the_lock_released(self, pre, caplog):
        pre.bhav = lambda syms: (_ for _ in ()).throw(RuntimeError("x" * 300))
        with caplog.at_level(logging.ERROR):
            out = pre.run(["A"])
        assert out == {"ok": False, "error": "x" * 200}
        assert pre.last["stage"] == "error" and pre.last["message"] == "x" * 200 and pre.last["errors"] == 1
        assert "precalculate_surprise_baselines failed" in caplog.text
        assert pre.pm._job_lock is False

    def test_lock_is_released_after_a_normal_run(self, pre):
        assert pre.run(["A"])["ok"] is True
        assert pre.pm._job_lock is False
        assert pre.run(["A"])["ok"] is True            # and the next run is accepted

    def test_a_stale_stop_request_is_cleared_at_the_start(self, pre):
        pre.pm.request_premarket_stop()
        out = pre.run(["A"])
        assert "stopped" not in out and out["ok"] is True


class TestPrecalcUniverse:
    def test_symbols_are_normalised_and_deduped(self, pre):
        pre.run([" tcs.ns", "TCS", "infy.bo", "", None, "M&M", "tcs"])
        assert pre.bhav_calls == [["TCS", "INFY", "M&M"]]
        assert pre.progress[0]["stage"] == "starting" and pre.progress[0]["total"] == 3

    def test_universe_is_capped_at_max_symbols(self, mkrig):
        r = mkrig(SURPRISE_MAX_SYMBOLS="2")
        out = r.run(["A", "B", "C", "D"])
        assert r.bhav_calls == [["A", "B"]] and out["symbols_requested"] == 2


class TestPrecalcFreshness:
    def test_fresh_universe_skips_the_recompute(self, pre):
        pre.fresh = lambda s: {"fresh": 9, "total": 10, "coverage": 0.9}
        out = pre.run([f"S{i}" for i in range(10)])
        assert out == {"ok": True, "skipped": True, "reason": "already_fresh_today", "symbols_requested": 10,
                       "fresh_coverage": 0.9, "computed": 0, "upserted": 0, "elapsed_sec": 0.0}
        assert pre.last["stage"] == "done" and pre.last["percent"] == 100 and pre.last["is_running"] is False
        assert pre.last["processed"] == 9 and pre.last["total"] == 10
        assert pre.last["message"] == ("Already fresh today: 9/10 baselines (90%) — skipped recompute "
                                       "(pass force=true to override)")
        assert pre.bhav_calls == [] and pre.upserts == [] and pre.pm._job_lock is False

    def test_coverage_percent_is_truncated(self, pre):
        pre.fresh = lambda s: {"fresh": 936, "total": 1000, "coverage": 0.936}
        pre.run(["A"])
        assert "(93%)" in pre.last["message"]

    def test_just_under_the_threshold_recomputes(self, pre):
        pre.fresh = lambda s: {"fresh": 89, "total": 100, "coverage": 0.89}
        out = pre.run(["A"])
        assert "skipped" not in out and pre.bhav_calls == [["A"]]

    def test_force_never_asks_for_freshness(self, pre):
        pre.fresh = lambda s: {"fresh": 10, "total": 10, "coverage": 1.0}
        out = pre.run(["A"], force=True)
        assert "skipped" not in out and pre.freshness_calls == [] and pre.bhav_calls == [["A"]]

    def test_freshness_is_asked_over_the_cleaned_universe(self, pre):
        pre.run([" a.ns", "A", "b"])
        assert pre.freshness_calls == [["A", "B"]]


class TestPrecalcBhavcopyStage:
    def test_bhavcopy_hits_are_flushed_first_and_the_rest_go_to_yfinance(self, pre):
        pre.bhav = lambda s: (rows_for("A", "B"), ["C", "D"])
        pre.yfbulk = lambda s: (rows_for("C"), ["D"])
        out = pre.run(["A", "B", "C", "D"])
        assert pre.yf_calls == [["C", "D"]] and pre.compute_calls == ["D"]
        assert pre.upserts == [["A", "B"], ["C"], ["D"]]
        assert out == {"ok": True, "symbols_requested": 4, "computed": 4, "errors": 0, "elapsed_sec": 0.0,
                       "table": "surprise_static_feed", "upserted": 4, "upserted_last_batch": 4, "workers": 4,
                       "source_bhavcopy": 2, "source_yfinance": 2, "source_market_data": 0}
        hit = next(p for p in pre.progress if str(p.get("message", "")).startswith("DB bhavcopy"))
        assert hit["message"] == "DB bhavcopy: 2 · yfinance left: 2"
        assert hit["percent"] == 50 and hit["processed"] == 2 and hit["computed"] == 2

    def test_bhavcopy_percent_has_a_floor_of_five(self, pre):
        pre.bhav = lambda s: (rows_for("A"), [f"S{i}" for i in range(49)])
        pre.yfbulk = lambda s: ([], [])
        pre.run([f"S{i}" for i in range(50)])
        hit = next(p for p in pre.progress if str(p.get("message", "")).startswith("DB bhavcopy"))
        assert hit["percent"] == 5

    def test_bhavcopy_miss_sends_the_whole_universe_to_yfinance(self, pre):
        pre.bhav = lambda s: ([], ["IGNORED"])
        pre.run(["A", "B"])
        assert pre.yf_calls == [["A", "B"]]

    def test_stage_order_for_a_clean_run(self, pre):
        pre.bhav = lambda s: (rows_for("A"), [])
        pre.yfbulk = lambda s: ([], [])
        pre.run(["A"])
        assert pre.stages == ["starting", "bhavcopy", "computing", "computing", "done"]
        assert pre.progress[0]["message"] == "Starting concurrent baselines for 1 symbols (workers=6)"
        assert pre.progress[1]["message"] == "Checking daily_bhavcopy fast path…"
        assert pre.progress[3]["message"] == "Bulk yfinance for 0 symbols…"


class TestPrecalcYfinanceAndResidual:
    def test_bulk_rows_need_no_residual_pool(self, pre, monkeypatch):
        monkeypatch.setattr(pre.pm, "ThreadPoolExecutor", boom)
        pre.yfbulk = lambda s: (rows_for(*s), [])
        out = pre.run(["A", "B"])
        assert out["computed"] == 2 and out["source_yfinance"] == 2 and pre.upserts == [["A", "B"]]

    def test_residual_outcomes_are_counted(self, pre, caplog):
        pre.compute = {"A": row_for("A"), "B": None, "C": RuntimeError("no data")}
        with caplog.at_level(logging.DEBUG, logger="surprise-premarket"):
            out = pre.run(["A", "B", "C"])
        assert sorted(pre.compute_calls) == ["A", "B", "C"]
        assert (out["computed"], out["errors"], out["source_yfinance"], out["upserted"]) == (1, 2, 1, 1)
        assert pre.upserts == [["A"]]
        assert "residual worker C: no data" in caplog.text
        assert pre.last["stage"] == "done" and pre.last["processed"] == 3 and pre.last["errors"] == 2
        residual = [p for p in pre.progress if str(p.get("message", "")).startswith("residual")]
        assert len(residual) == 3
        assert all(p["percent"] <= 99 and p["eta_sec"] is None for p in residual)   # frozen clock: no rate yet

    def test_rows_are_flushed_in_upsert_sized_batches(self, mkrig):
        r = mkrig(SURPRISE_UPSERT_BATCH="2")
        r.run(["A", "B", "C", "D", "E"])
        assert [len(b) for b in r.upserts] == [2, 2, 1]
        assert sorted(s for b in r.upserts for s in b) == ["A", "B", "C", "D", "E"]

    def test_eta_appears_once_the_clock_has_moved(self, pre):
        pre.clock.step = 1.0
        pre.run(["A", "B", "C", "D"])
        residual = [p for p in pre.progress if str(p.get("message", "")).startswith("residual")]
        assert residual and all(p["eta_sec"] is not None and p["elapsed_sec"] > 0 for p in residual)
        assert residual[-1]["percent"] == 99                      # never claims 100 before the final write
        assert pre.last["stage"] == "done" and pre.last["percent"] == 100

    def test_progress_is_logged_every_fifty_symbols(self, pre, caplog):
        syms = [f"S{i:02d}" for i in range(50)]
        with caplog.at_level(logging.INFO, logger="surprise-premarket"):
            pre.run(syms)
        assert "surprise premarket progress 50/50" in caplog.text
        assert "precalculate_surprise_baselines done: 50 computed, 0 errors" in caplog.text

    @pytest.mark.parametrize("n,want", [(1, 1), (2, 2), (3, 3), (10, 3)])
    def test_residual_pool_size(self, pre, monkeypatch, n, want):
        made = []
        real = pre.pm.ThreadPoolExecutor

        def spy(max_workers=None, **kw):
            made.append(max_workers)
            return real(max_workers=max_workers, **kw)

        monkeypatch.setattr(pre.pm, "ThreadPoolExecutor", spy)
        pre.run([f"S{i}" for i in range(n)])
        assert made == [want]

    def test_result_workers_follow_the_configured_cap(self, mkrig):
        r = mkrig(SURPRISE_PREMARKET_WORKERS="2")
        assert r.run(["A", "B", "C", "D"])["workers"] == 2
        assert r.last["message"].endswith("· 2 workers")

    def test_done_progress_carries_the_result(self, pre):
        out = pre.run(["A", "B"])
        done = pre.last
        assert done["stage"] == "done" and done["percent"] == 100 and done["eta_sec"] == 0
        assert done["processed"] == 2 and done["total"] == 2 and done["result"] == out
        assert done["message"] == "Done · 2 baselines · 0 errors · 0.0s · 2 workers"


class TestPrecalcStop:
    def test_stop_after_the_bulk_stage(self, pre):
        def yfbulk(syms):
            pre.pm.request_premarket_stop()
            return rows_for("A"), ["B"]

        pre.yfbulk = yfbulk
        out = pre.run(["A", "B"])
        assert out == {"ok": True, "stopped": True, "symbols_requested": 2, "computed": 1, "errors": 0,
                       "elapsed_sec": 0.0}
        assert pre.last["stage"] == "stopped" and pre.last["percent"] == 50
        assert pre.last["message"] == "Stopped by user at 1/2 (1 baselines saved)"
        assert pre.compute_calls == [] and pre.upserts == [["A"]]
        assert pre.pm.premarket_stop_requested() is False and pre.pm._job_lock is False

    def test_stop_with_an_empty_universe_reports_zero_percent(self, pre):
        def yfbulk(syms):
            pre.pm.request_premarket_stop()
            return [], []

        pre.yfbulk = yfbulk
        assert pre.run([])["stopped"] is True
        assert pre.last["percent"] == 0

    def test_stop_in_the_middle_of_the_residual_pass_keeps_finished_rows(self, pre, monkeypatch):
        pm = pre.pm
        calls = {"n": 0}

        def stop_on_third_check():
            calls["n"] += 1
            return calls["n"] >= 3        # 1: after bulk, 2: first future, 3: second future

        monkeypatch.setattr(pm, "premarket_stop_requested", stop_on_third_check)
        monkeypatch.setattr(pm, "as_completed", lambda futs: iter(sorted(futs, key=lambda f: futs[f])))
        out = pre.run(["A", "B", "C"])
        assert out["stopped"] is True and out["computed"] == 1
        assert pre.upserts == [["A"]]                        # the finished row was flushed, not discarded
        assert pre.last["stage"] == "stopped"
        assert pre.last["message"] == "Stopped by user at 1/3 (1 baselines saved)"
        assert pm._job_lock is False

    def test_stop_before_any_residual_row_finished_flushes_nothing(self, pre, monkeypatch):
        pm = pre.pm
        calls = {"n": 0}

        def stop_on_second_check():
            calls["n"] += 1
            return calls["n"] >= 2

        monkeypatch.setattr(pm, "premarket_stop_requested", stop_on_second_check)
        monkeypatch.setattr(pm, "as_completed", lambda futs: iter(sorted(futs, key=lambda f: futs[f])))
        out = pre.run(["A", "B"])
        assert out["stopped"] is True and out["computed"] == 0 and pre.upserts == []


class TestPrecalcEmptyAndDefensive:
    def test_empty_universe_runs_to_completion(self, pre):
        out = pre.run([])
        assert out["ok"] is True and out["symbols_requested"] == 0 and out["workers"] == 1
        assert out["computed"] == 0 and out["upserted"] == 0
        computing = [p for p in pre.progress if p["stage"] == "computing"]
        assert computing[0]["percent"] == 10             # `if total else 10`

    def test_zero_total_percent_fallbacks(self, pre):
        # Invariant-violating stubs (rows although nothing was requested) reach the defensive `else` arms
        # of the percent expressions; they must yield sane numbers instead of dividing by zero.
        pre.bhav = lambda s: (rows_for("X"), ["Y"])
        pre.yfbulk = lambda s: ([], ["Z"])
        out = pre.run([])
        assert out["ok"] is True
        bhav_progress = next(p for p in pre.progress if str(p.get("message", "")).startswith("DB bhavcopy"))
        assert bhav_progress["percent"] == 5
        residual = next(p for p in pre.progress if str(p.get("message", "")).startswith("residual"))
        assert residual["percent"] == 0


class TestPrecalcEndToEnd:
    def test_real_helpers_against_a_fake_engine_and_yfinance(self, dbpm, yf, monkeypatch):
        pm, db = dbpm
        install_clock(monkeypatch)
        monkeypatch.setitem(sys.modules, "surprise_schema", schema_fake(ensure_surprise_schema=lambda: {"ok": True}))
        monkeypatch.setattr(pm, "_yahoo_session", lambda: None)
        yf.results = [FMulti({"AAA.NS": ohlc(vol=100000.0), "BBB.NS": ohlc()})]
        out = pm.precalculate_surprise_baselines(["AAA", "BBB", "CCC"])
        # bhavcopy is empty, yfinance covers AAA/BBB, the single-symbol fallback finds nothing for CCC
        yf.history = None
        assert out["ok"] is True and out["symbols_requested"] == 3
        assert out["computed"] == 2 and out["errors"] == 1
        assert out["source_bhavcopy"] == 0 and out["source_yfinance"] == 2 and out["upserted"] == 2
        upsert = [c for c in db.calls if "ON CONFLICT (symbol) DO UPDATE" in c[0]]
        assert [r["symbol"] for r in upsert[0][1]] == ["AAA", "BBB"]
        assert pm.get_premarket_progress()["stage"] == "done"
        assert pm._job_lock is False


# ── fallback universe / default_universe_from_env ─────────────────────────────

class TestDefaultUniverse:
    def test_env_list_with_mixed_separators(self, load):
        m = load(SURPRISE_UNIVERSE="tcs, infy;;wipro , ")
        assert m.default_universe_from_env() == ["tcs", "infy", "wipro"]

    def test_scan_universe_is_the_second_choice(self, load):
        assert load(SCAN_UNIVERSE="A;B").default_universe_from_env() == ["A", "B"]
        assert load(SURPRISE_UNIVERSE="X", SCAN_UNIVERSE="A;B").default_universe_from_env() == ["X"]

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_blank_env_falls_back_to_the_liquid_list(self, load, caplog, raw):
        m = load(SURPRISE_UNIVERSE=raw)
        with caplog.at_level(logging.WARNING, logger="surprise-premarket"):
            got = m.default_universe_from_env()
        assert got == m._FALLBACK_LIQUID_UNIVERSE
        assert "using 250-symbol liquid fallback universe" in caplog.text

    def test_separator_only_env_yields_an_empty_list(self, load):
        # the env branch wins as soon as the raw string is non-blank, even if it holds no symbols
        assert load(SURPRISE_UNIVERSE=" ; , ").default_universe_from_env() == []

    def test_fallback_is_returned_as_a_copy(self, load):
        m = load()
        got = m.default_universe_from_env()
        got.append("MUTATED")
        assert "MUTATED" not in m._FALLBACK_LIQUID_UNIVERSE and len(m.default_universe_from_env()) == 250

    def test_fallback_universe_is_clean(self, load):
        m = load()
        uni = m._FALLBACK_LIQUID_UNIVERSE
        assert len(uni) == 250 and len(set(uni)) == 250
        assert all(re.fullmatch(r"[A-Z0-9&-]+", s) for s in uni)
        assert not (set(uni) & m._INDEX_SKIP)
        assert len(uni) <= m.MAX_SYMBOLS              # the whole fallback fits under the default job cap


# ── contracts with main.py and surprise_scanner.py ────────────────────────────

def _imported_names(path, module):
    tree = ast.parse(open(path, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            names.update(a.name for a in node.names)
    return names


def _load_scanner(monkeypatch):
    monkeypatch.delenv("HARD_FLOOR_LIQUIDITY", raising=False)
    spec = importlib.util.spec_from_file_location("scanner_for_contract_test",
                                                  os.path.join(_SERVICE, "surprise_scanner.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCrossModuleContracts:
    def test_every_name_main_imports_exists_and_is_callable(self, pm):
        names = _imported_names(os.path.join(_SERVICE, "main.py"), "surprise_premarket")
        assert {"precalculate_surprise_baselines", "get_premarket_progress", "default_universe_from_env",
                "request_premarket_stop"} <= names
        for n in names:
            assert callable(getattr(pm, n)), n

    def test_every_name_the_scanner_imports_exists(self, pm):
        names = _imported_names(os.path.join(_SERVICE, "surprise_scanner.py"), "surprise_premarket")
        assert names == {"premarket_stop_requested"}
        assert callable(pm.premarket_stop_requested)

    def test_entry_point_signatures(self, pm):
        sig = inspect.signature(pm.precalculate_surprise_baselines)
        assert list(sig.parameters) == ["symbols", "force"] and sig.parameters["force"].default is False
        for fn in (pm.get_premarket_progress, pm.default_universe_from_env, pm.request_premarket_stop,
                   pm.premarket_stop_requested, pm.clear_premarket_stop):
            assert not inspect.signature(fn).parameters

    def test_progress_reports_is_running_for_the_status_route(self, pm):
        assert pm.get_premarket_progress()["is_running"] is False
        pm._write_progress({"stage": "computing", "is_running": True})
        assert pm.get_premarket_progress()["is_running"] is True

    def test_result_is_a_mutable_dict_main_can_annotate(self, pre):
        out = pre.run(["A"])
        out["runner"] = "api-gateway"                # main.py adds runner/schema to the returned dict
        assert isinstance(out, dict)

    def _scanner_columns(self):
        tree = ast.parse(open(os.path.join(_SERVICE, "surprise_scanner.py"), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and node.value.startswith("SELECT symbol, prev_close"):
                cols = node.value.split(" FROM ")[0][len("SELECT "):]
                return [c.strip() for c in cols.split(",")]
        raise AssertionError("scanner SELECT not found")

    def test_baseline_rows_match_what_the_scanner_selects(self, bulk, monkeypatch):
        pm, yf = bulk
        wanted = {"symbol", "prev_close", "avg_15m_volume", "daily_atr", "high_52w", "dist_52w_pct", "sector",
                  "is_liquid"}
        scanner_cols = self._scanner_columns()
        assert set(scanner_cols) - {"updated_at"} == wanted          # the scanner reads exactly these

        yf.results = [FMulti({"AAA.NS": ohlc()})]
        yf_row = pm.bulk_baselines_from_yfinance(["AAA"])[0][0]
        yf.history = ohlc()
        single_row = pm.compute_baseline_for_symbol("AAA")
        conn = FakeConn(FakeDB(rows=bhav_rows(6)))
        bhav_row = pm.compute_baseline_from_bhavcopy("AAA", conn)
        bulk_db = FakeDB(rows=bulk_raw("AAA"))
        monkeypatch.setattr(pm, "_ss", None)
        monkeypatch.setattr(pm, "_db_url", lambda: "postgresql://h/db")
        monkeypatch.setattr(pm, "_engine", lambda app_name, **kw: bulk_db)
        bulk_row = pm.bulk_baselines_from_bhavcopy(["AAA"])[0][0]
        for row in (yf_row, single_row, bhav_row, bulk_row):
            assert set(row) == wanted

    def test_upsert_statement_writes_every_baseline_column(self, dbpm):
        pm, db = dbpm
        row = row_for("AAA")
        pm.upsert_baselines([row])
        sql = db.calls[0][0]
        for col in row:
            assert f":{col}" in sql, col

    @pytest.mark.parametrize("vol,liquid", [(50000.0, True), (49990.0, False)])
    def test_liquid_flag_agrees_with_the_scanners_hard_floor(self, bulk, monkeypatch, vol, liquid):
        pm, yf = bulk
        sc = _load_scanner(monkeypatch)
        assert pm.LIQUID_MIN_DAILY_TURNOVER == sc.HARD_FLOOR_LIQUIDITY
        yf.results = [FMulti({"AAA.NS": ohlc(close=100.0, vol=vol)})]
        row = pm.bulk_baselines_from_yfinance(["AAA"])[0][0]
        assert row["is_liquid"] is liquid
        # the scanner's own turnover estimate (15m volume x price x 25 slots) must clear the floor iff liquid
        scanner_turnover = row["avg_15m_volume"] * row["prev_close"] * 25
        assert (scanner_turnover >= sc.HARD_FLOOR_LIQUIDITY) is liquid

    def test_fallback_universe_survives_the_scanners_symbol_filter(self, pm, monkeypatch):
        sc = _load_scanner(monkeypatch)
        assert not [s for s in pm._FALLBACK_LIQUID_UNIVERSE if sc._DERIVATIVE_CONTRACT_RE.search(s)]
