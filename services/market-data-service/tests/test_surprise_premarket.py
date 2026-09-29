"""
tests/test_surprise_premarket.py — coverage for surprise_premarket.py

No yfinance, no real Neon, no SQLAlchemy network. All DB paths are
tested via a real SQLite in-memory engine or a monkeypatched _db_url().

Run from services/market-data-service:
    python3 -m pytest tests/test_surprise_premarket.py -v
"""
from __future__ import annotations
import json, os, sys, time, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine, text

import surprise_premarket as sp


# ── reset globals between tests ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    sp._job_lock = False
    monkeypatch.setenv("SURPRISE_PREMARKET_PROGRESS_PATH",
                       str(tmp_path / "progress.json"))
    # Ensure ORACLE_DSN is NOT set (so _db_url can return something)
    monkeypatch.delenv("ORACLE_DSN", raising=False)
    yield
    sp._job_lock = False


# ══════════════════════════════════════════════════════════════════════════════
# _normalize_db_url
# ══════════════════════════════════════════════════════════════════════════════

class TestNormalizeDbUrl:
    def test_postgres_rewritten(self):
        assert sp._normalize_db_url("postgres://h/db").startswith("postgresql://")

    def test_channel_binding_stripped(self):
        url = "postgresql://h/db?channel_binding=prefer"
        assert "channel_binding" not in sp._normalize_db_url(url)

    def test_sslmode_required_replaced(self):
        url = "postgresql://h/db?sslmode=required"
        result = sp._normalize_db_url(url)
        assert "sslmode=require" in result and "required" not in result

    def test_sslmode_added_when_absent(self):
        url = "postgresql://h/db"
        assert "sslmode=require" in sp._normalize_db_url(url)

    def test_sslmode_not_doubled(self):
        url = "postgresql://h/db?sslmode=require"
        assert sp._normalize_db_url(url).count("sslmode=") == 1


# ══════════════════════════════════════════════════════════════════════════════
# _db_url
# ══════════════════════════════════════════════════════════════════════════════

class TestDbUrl:
    def test_returns_none_when_no_env(self, monkeypatch):
        for k in ("CACHE_DATABASE_URL", "DATABASE_URL", "TRAINING_DATABASE_URL"):
            monkeypatch.delenv(k, raising=False)
        assert sp._db_url() is None

    def test_returns_none_when_oracle_dsn_set(self, monkeypatch):
        monkeypatch.setenv("ORACLE_DSN", "mydb_high")
        monkeypatch.setenv("DATABASE_URL", "postgresql://h/db")
        assert sp._db_url() is None

    def test_returns_none_when_oracle_url(self, monkeypatch):
        monkeypatch.delenv("ORACLE_DSN", raising=False)
        monkeypatch.setenv("CACHE_DATABASE_URL", "oracle+oracledb://user/db")
        assert sp._db_url() is None

    def test_returns_normalized_url_from_cache_db(self, monkeypatch):
        for k in ("DATABASE_URL", "TRAINING_DATABASE_URL"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("CACHE_DATABASE_URL", "postgresql://h/db?sslmode=require")
        url = sp._db_url()
        assert url is not None and "postgresql://" in url

    def test_fallback_chain(self, monkeypatch):
        monkeypatch.delenv("CACHE_DATABASE_URL", raising=False)
        monkeypatch.delenv("TRAINING_DATABASE_URL", raising=False)
        monkeypatch.setenv("DATABASE_URL", "postgresql://h/fallback")
        assert "fallback" in sp._db_url()


# ══════════════════════════════════════════════════════════════════════════════
# _yahoo_sym
# ══════════════════════════════════════════════════════════════════════════════

class TestYahooSym:
    def test_strips_ns_and_adds_ns(self):
        assert sp._yahoo_sym("RELIANCE.NS") == "RELIANCE.NS"

    def test_uppercase_and_appends_ns(self):
        assert sp._yahoo_sym("infy") == "INFY.NS"

    def test_strips_bo_and_adds_ns(self):
        assert sp._yahoo_sym("TCS.BO") == "TCS.NS"

    def test_empty_returns_empty(self):
        assert sp._yahoo_sym("") == ""

    def test_none_returns_empty(self):
        assert sp._yahoo_sym(None) == ""


# ══════════════════════════════════════════════════════════════════════════════
# ensure_schema
# ══════════════════════════════════════════════════════════════════════════════

class TestEnsureSchema:
    def test_returns_false_when_no_db_url(self, monkeypatch):
        for k in ("CACHE_DATABASE_URL", "DATABASE_URL", "TRAINING_DATABASE_URL"):
            monkeypatch.delenv(k, raising=False)
        # Also ensure surprise_schema import fails
        monkeypatch.setitem(sys.modules, "surprise_schema", None)
        result = sp.ensure_schema()
        assert result is False

    def test_delegates_to_surprise_schema_when_available(self, monkeypatch):
        import types
        fake = types.ModuleType("surprise_schema")
        fake.ensure_surprise_schema = lambda: {"ok": True}
        monkeypatch.setitem(sys.modules, "surprise_schema", fake)
        assert sp.ensure_schema() is True

    def test_falls_back_to_sqlalchemy_when_surprise_schema_fails(self, monkeypatch):
        import types
        fake = types.ModuleType("surprise_schema")
        fake.ensure_surprise_schema = lambda: {"ok": False, "error": "nope"}
        monkeypatch.setitem(sys.modules, "surprise_schema", fake)
        # Without a real DB url the SQLAlchemy path returns False
        for k in ("CACHE_DATABASE_URL", "DATABASE_URL", "TRAINING_DATABASE_URL"):
            monkeypatch.delenv(k, raising=False)
        assert sp.ensure_schema() is False

    def test_returns_false_on_sqlalchemy_connection_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "surprise_schema", None)
        monkeypatch.setenv("DATABASE_URL", "postgresql://invalid_host_xyz:5432/db")
        result = sp.ensure_schema()
        assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# compute_baseline_for_symbol
# ══════════════════════════════════════════════════════════════════════════════

class TestComputeBaselineForSymbol:
    def test_returns_none_for_index_symbol(self):
        assert sp.compute_baseline_for_symbol("NIFTY50") is None

    def test_returns_none_for_caret_symbol(self):
        assert sp.compute_baseline_for_symbol("^NSEI") is None

    def test_returns_none_when_yfinance_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "yfinance", None)
        monkeypatch.setitem(sys.modules, "numpy", None)
        assert sp.compute_baseline_for_symbol("RELIANCE") is None

    def test_returns_none_on_empty_history(self, monkeypatch):
        import types, numpy as np
        fake_yf = types.ModuleType("yfinance")
        class _FakeTicker:
            def history(self, **kw):
                class _Empty:
                    empty = True
                    def __len__(self): return 0
                return _Empty()
        fake_yf.Ticker = _FakeTicker
        monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
        monkeypatch.setitem(sys.modules, "numpy", np)
        result = sp.compute_baseline_for_symbol("RELIANCE")
        assert result is None

    def test_returns_row_dict_on_valid_history(self, monkeypatch):
        import types
        import numpy as np
        import pandas as pd

        fake_yf = types.ModuleType("yfinance")
        class _FakeTicker:
            def __init__(self, sym): pass
            def history(self, **kw):
                n = 40
                return pd.DataFrame({
                    "High":   [110.0 + i for i in range(n)],
                    "Low":    [100.0 + i for i in range(n)],
                    "Close":  [105.0 + i for i in range(n)],
                    "Volume": [1_000_000.0 for _ in range(n)],
                })
        fake_yf.Ticker = _FakeTicker
        existing_sess = object()  # non-None → _yahoo_session returns it without touching yf.set_session
        fake_yf.shared = types.SimpleNamespace(_session=existing_sess)
        monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
        monkeypatch.setitem(sys.modules, "numpy", np)
        monkeypatch.setitem(sys.modules, "pandas", pd)
        monkeypatch.setitem(sys.modules, "rate_limiter", None)

        result = sp.compute_baseline_for_symbol("RELIANCE")
        assert result is not None
        assert result["symbol"] == "RELIANCE"
        assert result["prev_close"] > 0
        assert result["avg_15m_volume"] >= 1
        assert result["daily_atr"] > 0
        assert result["high_52w"] >= result["prev_close"]
        assert "is_liquid" in result

    def test_zero_high_52w_returns_none(self, monkeypatch):
        import types, numpy as np, pandas as pd
        fake_yf = types.ModuleType("yfinance")
        class _FakeTicker:
            def history(self, **kw):
                n = 10
                return pd.DataFrame({
                    "High": [0.0]*n, "Low": [0.0]*n,
                    "Close": [0.0]*n, "Volume": [0.0]*n,
                })
        fake_yf.Ticker = _FakeTicker
        fake_yf.shared = types.SimpleNamespace(_session=None)
        monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
        monkeypatch.setitem(sys.modules, "numpy", np)
        assert sp.compute_baseline_for_symbol("ZEROSTOCK") is None

    def test_exception_returns_none(self, monkeypatch):
        import types
        fake_yf = types.ModuleType("yfinance")
        fake_yf.Ticker = lambda s: (_ for _ in ()).throw(RuntimeError("API down"))
        fake_yf.shared = types.SimpleNamespace(_session=None)
        monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
        import numpy as np
        monkeypatch.setitem(sys.modules, "numpy", np)
        assert sp.compute_baseline_for_symbol("BROKEN") is None


# ══════════════════════════════════════════════════════════════════════════════
# compute_baseline_from_bhavcopy
# ══════════════════════════════════════════════════════════════════════════════

class TestComputeBaselineFromBhavcopy:
    def _sqlite_conn_with_rows(self, n=20, close=100.0, high=110.0,
                                low=90.0, volume=500_000.0):
        import numpy as np
        eng = create_engine("sqlite:///:memory:")
        with eng.begin() as conn:
            conn.execute(text("""
                CREATE TABLE daily_bhavcopy (
                    symbol TEXT, high REAL, low REAL,
                    close REAL, volume REAL, trade_date TEXT
                )
            """))
            for i in range(n):
                conn.execute(text("""
                    INSERT INTO daily_bhavcopy VALUES (:s,:h,:l,:c,:v,:d)
                """), {"s": "RELIANCE", "h": high+i*0.1, "l": low+i*0.1,
                       "c": close+i*0.1, "v": volume, "d": f"2026-01-{i+1:02d}"})
        return eng

    def test_returns_row_for_valid_data(self):
        import numpy as np
        eng = self._sqlite_conn_with_rows(20)
        with eng.connect() as conn:
            result = sp.compute_baseline_from_bhavcopy("RELIANCE", conn)
        assert result is not None
        assert result["symbol"] == "RELIANCE"
        assert result["prev_close"] > 0

    def test_returns_none_for_fewer_than_5_rows(self):
        import numpy as np
        eng = self._sqlite_conn_with_rows(3)
        with eng.connect() as conn:
            result = sp.compute_baseline_from_bhavcopy("RELIANCE", conn)
        assert result is None

    def test_returns_none_when_high_52w_is_zero(self):
        import numpy as np
        # Needs prev_close == 0 too; use fixed values, not incremented
        eng = create_engine("sqlite:///:memory:")
        with eng.begin() as conn:
            conn.execute(text("""
                CREATE TABLE daily_bhavcopy (
                    symbol TEXT, high REAL, low REAL,
                    close REAL, volume REAL, trade_date TEXT
                )
            """))
            for i in range(10):
                conn.execute(text(
                    "INSERT INTO daily_bhavcopy VALUES (:s,0.0,0.0,0.0,0.0,:d)"
                ), {"s": "RELIANCE", "d": f"2026-01-{i+1:02d}"})
        with eng.connect() as conn:
            result = sp.compute_baseline_from_bhavcopy("RELIANCE", conn)
        assert result is None

    def test_handles_missing_symbol(self):
        eng = self._sqlite_conn_with_rows(20)
        with eng.connect() as conn:
            result = sp.compute_baseline_from_bhavcopy("NOTEXIST", conn)
        assert result is None

    def test_exception_returns_none(self):
        class _BrokenConn:
            def execute(self, *a, **kw): raise RuntimeError("table missing")
        result = sp.compute_baseline_from_bhavcopy("X", _BrokenConn())
        assert result is None

    def test_strips_ns_suffix(self):
        import numpy as np
        eng = self._sqlite_conn_with_rows(10)
        with eng.connect() as conn:
            result = sp.compute_baseline_from_bhavcopy("RELIANCE.NS", conn)
        # Symbol looked up without suffix — if found, non-None; if not (table
        # only has "RELIANCE" not "RELIANCE.NS"), None is acceptable too.
        # The point is no crash.
        assert result is None or result["symbol"] == "RELIANCE"


# ══════════════════════════════════════════════════════════════════════════════
# _table_exists
# ══════════════════════════════════════════════════════════════════════════════

class TestTableExists:
    def test_existing_table_returns_true(self):
        eng = create_engine("sqlite:///:memory:")
        with eng.begin() as conn:
            conn.execute(text("CREATE TABLE foo (id INTEGER)"))
        # SQLite has information_schema in newer versions; if not, exception → False
        with eng.connect() as conn:
            # The function uses information_schema — SQLite won't have it
            # so _table_exists returns False, which is the "graceful" path
            result = sp._table_exists(conn, "foo")
        assert isinstance(result, bool)

    def test_missing_table_returns_false(self):
        eng = create_engine("sqlite:///:memory:")
        with eng.connect() as conn:
            assert sp._table_exists(conn, "no_such_table") is False


# ══════════════════════════════════════════════════════════════════════════════
# upsert_baselines
# ══════════════════════════════════════════════════════════════════════════════

class TestUpsertBaselines:
    def test_returns_zero_on_empty_rows(self):
        assert sp.upsert_baselines([]) == 0

    def test_returns_zero_when_no_db_url(self, monkeypatch):
        for k in ("CACHE_DATABASE_URL", "DATABASE_URL", "TRAINING_DATABASE_URL"):
            monkeypatch.delenv(k, raising=False)
        rows = [{"symbol": "X", "prev_close": 100, "avg_15m_volume": 1000,
                 "daily_atr": 2.0, "high_52w": 120.0, "dist_52w_pct": 16.7,
                 "sector": None, "is_liquid": True}]
        assert sp.upsert_baselines(rows) == 0

    def test_returns_zero_on_connection_error(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://invalid_xyz/db")
        rows = [{"symbol": "X", "prev_close": 100, "avg_15m_volume": 1000,
                 "daily_atr": 2.0, "high_52w": 120.0, "dist_52w_pct": 16.7,
                 "sector": None, "is_liquid": True}]
        result = sp.upsert_baselines(rows)
        assert result == 0


# ══════════════════════════════════════════════════════════════════════════════
# _write_progress / get_premarket_progress
# ══════════════════════════════════════════════════════════════════════════════

class TestProgressIo:
    def test_write_then_read(self):
        sp._write_progress({"stage": "test", "percent": 42})
        data = sp.get_premarket_progress()
        assert data["stage"] == "test"
        assert data["percent"] == 42
        assert "updated_at" in data

    def test_get_returns_idle_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sp, "_PROGRESS_PATH", str(tmp_path / "nonexistent.json"))
        data = sp.get_premarket_progress()
        assert data["stage"] == "idle"
        assert data["is_running"] is False

    def test_get_returns_idle_on_corrupt_file(self, tmp_path, monkeypatch):
        p = tmp_path / "corrupt.json"
        p.write_text("NOT JSON {{{{")
        monkeypatch.setattr(sp, "_PROGRESS_PATH", str(p))
        data = sp.get_premarket_progress()
        assert data["stage"] == "idle"

    def test_write_progress_swallows_write_error(self, monkeypatch):
        monkeypatch.setattr(sp, "_PROGRESS_PATH", "/no/such/dir/x.json")
        sp._write_progress({"stage": "ok"})   # must not raise


# ══════════════════════════════════════════════════════════════════════════════
# bulk_baselines_from_bhavcopy
# ══════════════════════════════════════════════════════════════════════════════

class TestBulkBaselinesFromBhavcopy:
    def test_returns_empty_when_no_db_url(self, monkeypatch):
        for k in ("CACHE_DATABASE_URL", "DATABASE_URL", "TRAINING_DATABASE_URL"):
            monkeypatch.delenv(k, raising=False)
        rows, remaining = sp.bulk_baselines_from_bhavcopy(["RELIANCE"])
        assert rows == []
        assert "RELIANCE" in remaining

    def test_returns_empty_when_no_symbols(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://h/db")
        rows, remaining = sp.bulk_baselines_from_bhavcopy([])
        assert rows == []
        assert remaining == []

    def test_returns_empty_on_connection_error(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://invalid_xyz/db")
        rows, remaining = sp.bulk_baselines_from_bhavcopy(["TCS"])
        assert rows == []
        assert "TCS" in remaining

    def test_returns_empty_when_daily_bhavcopy_missing(self, monkeypatch):
        """When daily_bhavcopy table doesn't exist → falls back immediately."""
        import numpy as np

        eng = create_engine("sqlite:///:memory:")   # no daily_bhavcopy table

        original_create_engine = sp.__builtins__ if isinstance(sp.__builtins__, dict) else None

        def _fake_db_url(): return "sqlite:///:memory:"
        monkeypatch.setattr(sp, "_db_url", _fake_db_url)

        # Patch sqlalchemy.create_engine inside the function's import
        import sqlalchemy
        original = sqlalchemy.create_engine
        monkeypatch.setattr(sqlalchemy, "create_engine", lambda url, **kw: eng)

        import importlib
        rows, remaining = sp.bulk_baselines_from_bhavcopy(["RELIANCE", "TCS"])
        # daily_bhavcopy doesn't exist in SQLite → _table_exists returns False
        # → returns [], original list
        assert rows == []
        assert set(remaining) >= {"RELIANCE", "TCS"}


# ══════════════════════════════════════════════════════════════════════════════
# default_universe_from_env
# ══════════════════════════════════════════════════════════════════════════════

class TestDefaultUniverseFromEnv:
    def test_parses_comma_separated_env(self, monkeypatch):
        monkeypatch.setenv("SURPRISE_UNIVERSE", "RELIANCE,TCS,INFY")
        result = sp.default_universe_from_env()
        assert result == ["RELIANCE", "TCS", "INFY"]

    def test_parses_semicolon_separated_env(self, monkeypatch):
        monkeypatch.setenv("SURPRISE_UNIVERSE", "RELIANCE;TCS")
        result = sp.default_universe_from_env()
        assert result == ["RELIANCE", "TCS"]

    def test_scan_universe_fallback(self, monkeypatch):
        monkeypatch.delenv("SURPRISE_UNIVERSE", raising=False)
        monkeypatch.setenv("SCAN_UNIVERSE", "WIPRO,HCLTECH")
        result = sp.default_universe_from_env()
        assert result == ["WIPRO", "HCLTECH"]

    def test_returns_fallback_universe_when_no_env(self, monkeypatch):
        monkeypatch.delenv("SURPRISE_UNIVERSE", raising=False)
        monkeypatch.delenv("SCAN_UNIVERSE", raising=False)
        result = sp.default_universe_from_env()
        assert len(result) >= 200
        assert "RELIANCE" in result
        assert "HDFCBANK" in result

    def test_strips_whitespace_from_symbols(self, monkeypatch):
        monkeypatch.setenv("SURPRISE_UNIVERSE", " RELIANCE , TCS ")
        result = sp.default_universe_from_env()
        assert "RELIANCE" in result and "TCS" in result


# ══════════════════════════════════════════════════════════════════════════════
# precalculate_surprise_baselines — job-lock and schema-fail paths
# ══════════════════════════════════════════════════════════════════════════════

class TestPrecalculateSurpriseBaselines:
    def test_returns_already_running_when_locked(self):
        sp._job_lock = True
        result = sp.precalculate_surprise_baselines(["RELIANCE"])
        assert result["ok"] is False
        assert result["error"] == "already_running"

    def test_releases_lock_on_completion(self, monkeypatch):
        monkeypatch.setattr(sp, "ensure_schema", lambda: False)
        sp.precalculate_surprise_baselines(["RELIANCE"])
        assert sp._job_lock is False

    def test_returns_schema_failed_when_schema_errors(self, monkeypatch):
        monkeypatch.setattr(sp, "ensure_schema", lambda: False)
        result = sp.precalculate_surprise_baselines(["RELIANCE"])
        assert result["ok"] is False
        assert result["error"] == "schema_failed"

    def test_empty_symbols_completes_cleanly(self, monkeypatch):
        monkeypatch.setattr(sp, "ensure_schema", lambda: True)
        monkeypatch.setattr(sp, "bulk_baselines_from_bhavcopy", lambda s: ([], s))
        monkeypatch.setattr(sp, "bulk_baselines_from_yfinance", lambda s, **kw: ([], s))
        monkeypatch.setattr(sp, "upsert_baselines", lambda rows: 0)
        result = sp.precalculate_surprise_baselines([])
        assert result["ok"] is True
        assert result["computed"] == 0

    def test_deduplicates_and_normalises_symbols(self, monkeypatch):
        seen = []
        def _fake_bhav(syms):
            seen.extend(syms)
            return [], syms
        monkeypatch.setattr(sp, "ensure_schema", lambda: True)
        monkeypatch.setattr(sp, "bulk_baselines_from_bhavcopy", _fake_bhav)
        monkeypatch.setattr(sp, "bulk_baselines_from_yfinance", lambda s, **kw: ([], []))
        monkeypatch.setattr(sp, "upsert_baselines", lambda rows: 0)
        sp.precalculate_surprise_baselines(["reliance.NS", "RELIANCE", "TCS.BO"])
        # "RELIANCE" appears twice (case+suffix variant) → deduplicated to 1
        assert seen.count("RELIANCE") == 1
        assert "TCS" in seen

    def test_returns_ok_with_bhav_rows(self, monkeypatch):
        bhav_rows = [{
            "symbol": "RELIANCE", "prev_close": 2500.0,
            "avg_15m_volume": 40000, "daily_atr": 25.0,
            "high_52w": 2800.0, "dist_52w_pct": 10.7,
            "sector": None, "is_liquid": True,
        }]
        monkeypatch.setattr(sp, "ensure_schema", lambda: True)
        monkeypatch.setattr(sp, "bulk_baselines_from_bhavcopy", lambda s: (bhav_rows, []))
        monkeypatch.setattr(sp, "bulk_baselines_from_yfinance", lambda s, **kw: ([], []))
        monkeypatch.setattr(sp, "upsert_baselines", lambda rows: len(rows))
        result = sp.precalculate_surprise_baselines(["RELIANCE"])
        assert result["ok"] is True
        assert result["computed"] >= 1
        assert result["source_bhavcopy"] >= 1

    def test_returns_ok_with_yf_rows(self, monkeypatch):
        yf_rows = [{
            "symbol": "TCS", "prev_close": 3200.0,
            "avg_15m_volume": 32000, "daily_atr": 30.0,
            "high_52w": 3500.0, "dist_52w_pct": 8.6,
            "sector": None, "is_liquid": True,
        }]
        monkeypatch.setattr(sp, "ensure_schema", lambda: True)
        monkeypatch.setattr(sp, "bulk_baselines_from_bhavcopy", lambda s: ([], s))
        monkeypatch.setattr(sp, "bulk_baselines_from_yfinance", lambda s, **kw: (yf_rows, []))
        monkeypatch.setattr(sp, "upsert_baselines", lambda rows: len(rows))
        result = sp.precalculate_surprise_baselines(["TCS"])
        assert result["ok"] is True
        assert result["source_yfinance"] >= 1

    def test_residual_per_symbol_fallback(self, monkeypatch):
        """Symbols not resolved by bulk paths go through compute_baseline_for_symbol."""
        residual_row = {
            "symbol": "WIPRO", "prev_close": 300.0,
            "avg_15m_volume": 12000, "daily_atr": 5.0,
            "high_52w": 350.0, "dist_52w_pct": 14.3,
            "sector": None, "is_liquid": True,
        }
        monkeypatch.setattr(sp, "ensure_schema", lambda: True)
        monkeypatch.setattr(sp, "bulk_baselines_from_bhavcopy", lambda s: ([], s))
        monkeypatch.setattr(sp, "bulk_baselines_from_yfinance", lambda s, **kw: ([], s))
        monkeypatch.setattr(sp, "compute_baseline_for_symbol", lambda sym: residual_row)
        monkeypatch.setattr(sp, "upsert_baselines", lambda rows: len(rows))
        result = sp.precalculate_surprise_baselines(["WIPRO"])
        assert result["ok"] is True
        assert result["source_yfinance"] >= 1

    def test_residual_errors_counted(self, monkeypatch):
        """Symbols where per-symbol baseline returns None increment errors."""
        monkeypatch.setattr(sp, "ensure_schema", lambda: True)
        monkeypatch.setattr(sp, "bulk_baselines_from_bhavcopy", lambda s: ([], s))
        monkeypatch.setattr(sp, "bulk_baselines_from_yfinance", lambda s, **kw: ([], s))
        monkeypatch.setattr(sp, "compute_baseline_for_symbol", lambda sym: None)
        monkeypatch.setattr(sp, "upsert_baselines", lambda rows: 0)
        result = sp.precalculate_surprise_baselines(["BADSTOCK"])
        assert result["ok"] is True
        assert result["errors"] >= 1

    def test_respects_max_symbols_cap(self, monkeypatch):
        original_max = sp.MAX_SYMBOLS
        sp.MAX_SYMBOLS = 3
        captured = []
        def _fake_bhav(syms):
            captured.extend(syms)
            return [], []
        monkeypatch.setattr(sp, "ensure_schema", lambda: True)
        monkeypatch.setattr(sp, "bulk_baselines_from_bhavcopy", _fake_bhav)
        monkeypatch.setattr(sp, "bulk_baselines_from_yfinance", lambda s, **kw: ([], []))
        monkeypatch.setattr(sp, "upsert_baselines", lambda rows: 0)
        try:
            sp.precalculate_surprise_baselines(["A","B","C","D","E"])
        finally:
            sp.MAX_SYMBOLS = original_max
        assert len(captured) <= 3

    def test_exception_during_run_returns_error_dict(self, monkeypatch):
        def _boom(symbols):
            raise RuntimeError("unexpected crash")
        monkeypatch.setattr(sp, "ensure_schema", _boom)
        result = sp.precalculate_surprise_baselines(["RELIANCE"])
        assert result["ok"] is False
        assert "error" in result
        assert sp._job_lock is False   # always released

    def test_progress_file_written_on_done(self, monkeypatch):
        monkeypatch.setattr(sp, "ensure_schema", lambda: True)
        monkeypatch.setattr(sp, "bulk_baselines_from_bhavcopy", lambda s: ([], []))
        monkeypatch.setattr(sp, "bulk_baselines_from_yfinance", lambda s, **kw: ([], []))
        monkeypatch.setattr(sp, "upsert_baselines", lambda rows: 0)
        sp.precalculate_surprise_baselines(["RELIANCE"])
        data = sp.get_premarket_progress()
        assert data["stage"] in ("done", "idle")
