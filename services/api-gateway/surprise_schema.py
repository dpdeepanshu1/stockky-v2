"""
Surprise static feed schema — Neon/Postgres (Render) AND Oracle Autonomous DB (Oracle VM).

Single source of truth for table name + columns + portable SQL, used by:
  - surprise_premarket.py (writer / upsert)
  - surprise_scanner.py   (reader / select)

Table: surprise_static_feed
Columns:
  symbol, prev_close, avg_15m_volume, daily_atr, high_52w, dist_52w_pct,
  sector, is_liquid, updated_at

Backend selection (identical convention to oracle_compat.py / kv_cache.py):
  ORACLE_DSN set              -> Oracle Autonomous DB (wallet/DSN via ORACLE_*)
  otherwise                   -> CACHE_DATABASE_URL | DATABASE_URL | TRAINING_DATABASE_URL

Design rule: the Postgres path below is byte-for-byte what it always was
(same DDL text, same INSERT ... ON CONFLICT text, same engine kwargs), so
Render cannot regress. Oracle is an additive branch.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Optional

# Neon/Postgres <-> Oracle portability shim (sits next to this file in the
# api-gateway service). Imported defensively: if it is missing we simply behave
# like the original Postgres-only module.
try:
    import oracle_compat as _oc
except Exception:  # pragma: no cover
    _oc = None

logger = logging.getLogger("surprise-schema")

TABLE_NAME = "surprise_static_feed"

# ── Postgres DDL (unchanged from the original module) ───────────────────────
# Defaults allow CREATE/INSERT even when a metric is missing
DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS surprise_static_feed (
        symbol VARCHAR(30) PRIMARY KEY,
        prev_close NUMERIC(12, 2) NOT NULL DEFAULT 0,
        avg_15m_volume BIGINT NOT NULL DEFAULT 10000,
        daily_atr NUMERIC(12, 2) NOT NULL DEFAULT 0.0,
        high_52w NUMERIC(12, 2) NOT NULL DEFAULT 0,
        dist_52w_pct NUMERIC(8, 2) NOT NULL DEFAULT 100,
        sector VARCHAR(80),
        is_liquid BOOLEAN DEFAULT TRUE,
        updated_at TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_surprise_static_sym ON surprise_static_feed(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_surprise_static_updated ON surprise_static_feed(updated_at)",
]

# ── Oracle DDL ─────────────────────────────────────────────────────────────
# Differences forced by Oracle: VARCHAR2 / NUMBER / TIMESTAMP / SYSTIMESTAMP,
# DEFAULT must precede NOT NULL, there is no BOOLEAN (NUMBER(1) with 1/0), and
# there is no "IF NOT EXISTS" before 23c — exec_ddl_safe() swallows ORA-00955
# (table exists) and ORA-01408 (column already indexed, which the redundant
# symbol index hits because the primary key already indexes it).
DDL_STATEMENTS_ORACLE = [
    """
    CREATE TABLE surprise_static_feed (
        symbol VARCHAR2(30) PRIMARY KEY,
        prev_close NUMBER(12, 2) DEFAULT 0 NOT NULL,
        avg_15m_volume NUMBER(19) DEFAULT 10000 NOT NULL,
        daily_atr NUMBER(12, 2) DEFAULT 0 NOT NULL,
        high_52w NUMBER(12, 2) DEFAULT 0 NOT NULL,
        dist_52w_pct NUMBER(8, 2) DEFAULT 100 NOT NULL,
        sector VARCHAR2(80),
        is_liquid NUMBER(1) DEFAULT 1,
        updated_at TIMESTAMP DEFAULT SYSTIMESTAMP
    )
    """,
    "CREATE INDEX idx_surprise_static_updated ON surprise_static_feed(updated_at)",
]

# Explicit column list — never rely on SELECT * for scanner logic
SELECT_COLUMNS = (
    "symbol, prev_close, avg_15m_volume, daily_atr, high_52w, "
    "dist_52w_pct, sector, is_liquid, updated_at"
)

INSERT_COLUMNS = (
    "symbol, prev_close, avg_15m_volume, daily_atr, high_52w, "
    "dist_52w_pct, sector, is_liquid, updated_at"
)

# Bind keys the writer supplies per row (updated_at is set by SQL, not bound)
ROW_KEYS = (
    "symbol", "prev_close", "avg_15m_volume", "daily_atr",
    "high_52w", "dist_52w_pct", "sector", "is_liquid",
)


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if "channel_binding=" in url:
        url = re.sub(r"([&?])channel_binding=[^&]*", r"\1", url)
        url = url.replace("?&", "?").rstrip("?&")
    url = re.sub(r"(?i)([?&]sslmode=)required\b", r"\1require", url)
    if "sslmode=" not in url.lower():
        url = url + ("&" if "?" in url else "?") + "sslmode=require"
    return url


def is_oracle() -> bool:
    """True when this process should store the surprise feed in Oracle ADB."""
    if _oc is not None:
        try:
            return _oc.oracle_is_configured(_raw_url() or "")
        except Exception:
            pass
    return bool(os.environ.get("ORACLE_DSN"))


def dialect() -> str:
    """'oracle' | 'postgresql' — which SQL flavour to emit."""
    return "oracle" if is_oracle() else "postgresql"


def _raw_url() -> str:
    return (
        os.getenv("CACHE_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("TRAINING_DATABASE_URL")
        or ""
    )


def database_url() -> Optional[str]:
    """Resolved DB URL, or None when no backend is configured at all.

    Oracle VM: returns the scheme-only sentinel 'oracle+oracledb://' (or the
    full oracle URL when one was given). Credentials/DSN/wallet ride in via
    connect_args in make_engine() — exactly like kv_cache.py. This used to
    return None on Oracle, which is what made the whole surprise feature a
    permanent no-op there (503 from /surprise/premarket, "static cache empty"
    from the scanner). Render/Neon behaviour is unchanged.
    """
    url = _raw_url()
    if is_oracle():
        return url if url.lower().startswith("oracle") else "oracle+oracledb://"
    return _normalize_db_url(url) if url else None


def ddl_statements(dial: Optional[str] = None) -> list:
    dial = dial or dialect()
    return DDL_STATEMENTS_ORACLE if dial == "oracle" else DDL_STATEMENTS


def now_func(dial: Optional[str] = None) -> str:
    dial = dial or dialect()
    return "SYSTIMESTAMP" if dial == "oracle" else "NOW()"


def make_engine(app_name: str = "stockky-surprise"):
    """Build a SQLAlchemy engine for whichever backend is configured.

    Returns None when nothing is configured. Callers must use this instead of
    create_engine(database_url()) — the Postgres connect_args (connect_timeout,
    application_name) are psycopg2-only and would break the Oracle driver.
    """
    url = database_url()
    if not url:
        return None
    from sqlalchemy import create_engine

    if is_oracle():
        if _oc is None:
            logger.warning("surprise: ORACLE_DSN set but oracle_compat.py missing")
            return None
        eng, _ = _oc.build_oracle_engine(
            url,
            db_pool_size=os.getenv("SURPRISE_DB_POOL_SIZE", "2"),
            db_max_overflow=os.getenv("SURPRISE_DB_MAX_OVERFLOW", "1"),
            db_pool_recycle=os.getenv("SURPRISE_DB_POOL_RECYCLE", "300"),
            db_pool_timeout=os.getenv("SURPRISE_DB_POOL_TIMEOUT", "15"),
        )
        return eng

    # ── Neon / Postgres — original kwargs, unchanged ──
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=1,
        pool_timeout=8,
        connect_args={"connect_timeout": 8, "application_name": app_name},
    )


_ENGINE_CACHE: dict = {}
_ENGINE_LOCK = threading.Lock()


def shared_engine(app_name: str = "stockky-surprise"):
    """Process-wide cached engine — build the pool ONCE, not per operation.

    Mirrors hotpicks_schema.shared_engine. make_engine() opens a brand-new pool
    on every call (full TCP+TLS handshake, and wallet/mTLS on Oracle), and the
    Surprise scan/premarket paths were calling it per operation — some paths even
    leaked the engine (never disposed). On Neon's free tier (~20 connection cap)
    that churn/leak exhausts connections and makes Surprise (and everything else
    sharing the DB) start failing. Caching by (app_name, resolved URL) keeps one
    warm pool per process.

    Callers must NOT dispose() an engine obtained from here — it is shared.
    """
    key = (app_name, database_url() or "")
    with _ENGINE_LOCK:
        eng = _ENGINE_CACHE.get(key)
        if eng is None:
            eng = make_engine(app_name)
            if eng is not None:
                _ENGINE_CACHE[key] = eng
        return eng


def dispose_shared_engines() -> None:
    """Drop all cached pools (test teardown / a deliberate reconnect)."""
    with _ENGINE_LOCK:
        for eng in _ENGINE_CACHE.values():
            try:
                eng.dispose()
            except Exception:
                pass
        _ENGINE_CACHE.clear()


def table_exists_sql(dial: Optional[str] = None) -> str:
    """Bind :tbl (lower-case name). Oracle stores unquoted names upper-case."""
    dial = dial or dialect()
    if dial == "oracle":
        return "SELECT COUNT(*) FROM user_tables WHERE table_name = UPPER(:tbl)"
    return (
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = :tbl"
    )


def upsert_sql(dial: Optional[str] = None) -> str:
    """Upsert one row keyed on symbol. Binds: the eight ROW_KEYS.

    Postgres -> INSERT ... ON CONFLICT (symbol) DO UPDATE   (original text)
    Oracle   -> MERGE ... USING (SELECT ... FROM dual)
    Safe for executemany in both dialects.
    """
    dial = dial or dialect()
    if dial == "oracle":
        return """
            MERGE INTO surprise_static_feed d
            USING (
                SELECT :symbol AS symbol, :prev_close AS prev_close,
                       :avg_15m_volume AS avg_15m_volume, :daily_atr AS daily_atr,
                       :high_52w AS high_52w, :dist_52w_pct AS dist_52w_pct,
                       :sector AS sector, :is_liquid AS is_liquid
                FROM dual
            ) s ON (d.symbol = s.symbol)
            WHEN MATCHED THEN UPDATE SET
                d.prev_close = s.prev_close,
                d.avg_15m_volume = s.avg_15m_volume,
                d.daily_atr = s.daily_atr,
                d.high_52w = s.high_52w,
                d.dist_52w_pct = s.dist_52w_pct,
                d.sector = COALESCE(s.sector, d.sector),
                d.is_liquid = s.is_liquid,
                d.updated_at = SYSTIMESTAMP
            WHEN NOT MATCHED THEN INSERT
                (symbol, prev_close, avg_15m_volume, daily_atr, high_52w,
                 dist_52w_pct, sector, is_liquid, updated_at)
            VALUES
                (s.symbol, s.prev_close, s.avg_15m_volume, s.daily_atr,
                 s.high_52w, s.dist_52w_pct, s.sector, s.is_liquid, SYSTIMESTAMP)
        """
    return """
        INSERT INTO surprise_static_feed
            (symbol, prev_close, avg_15m_volume, daily_atr, high_52w, dist_52w_pct, sector, is_liquid, updated_at)
        VALUES
            (:symbol, :prev_close, :avg_15m_volume, :daily_atr, :high_52w, :dist_52w_pct, :sector, :is_liquid, NOW())
        ON CONFLICT (symbol) DO UPDATE SET
            prev_close = EXCLUDED.prev_close,
            avg_15m_volume = EXCLUDED.avg_15m_volume,
            daily_atr = EXCLUDED.daily_atr,
            high_52w = EXCLUDED.high_52w,
            dist_52w_pct = EXCLUDED.dist_52w_pct,
            sector = COALESCE(EXCLUDED.sector, surprise_static_feed.sector),
            is_liquid = EXCLUDED.is_liquid,
            updated_at = NOW()
    """


def adapt_rows(rows: list, dial: Optional[str] = None) -> list:
    """Make bind values safe for the target driver.

    Oracle has no BOOLEAN: is_liquid is NUMBER(1), and python-oracledb will not
    bind a bool into it. Convert to 1/0 there; leave Postgres rows untouched so
    the Render path is bit-identical.
    """
    dial = dial or dialect()
    if dial != "oracle":
        return rows
    out = []
    for r in rows:
        r2 = dict(r)
        if "is_liquid" in r2 and r2["is_liquid"] is not None:
            r2["is_liquid"] = 1 if r2["is_liquid"] else 0
        out.append(r2)
    return out


def ensure_surprise_schema() -> dict:
    """Create table/indexes if missing. Safe to call on every premarket / scan."""
    url = database_url()
    if not url:
        return {"ok": False, "error": "No DATABASE_URL / CACHE_DATABASE_URL configured"}
    eng = None
    try:
        from sqlalchemy import text

        dial = dialect()
        eng = make_engine("stockky-surprise-schema")
        if eng is None:
            return {"ok": False, "error": "Could not build a database engine"}

        if dial == "oracle":
            # Oracle auto-commits DDL and has no IF NOT EXISTS: run each
            # statement in its own transaction and swallow "already exists".
            for stmt in ddl_statements(dial):
                s = stmt.strip()
                if s and _oc is not None:
                    _oc.exec_ddl_safe(eng, s, "oracle")
        else:
            with eng.begin() as conn:
                for stmt in ddl_statements(dial):
                    s = stmt.strip()
                    if s:
                        conn.execute(text(s))

        logger.info("surprise_static_feed schema ready (backend=%s)", dial)
        return {"ok": True, "table": TABLE_NAME, "backend": dial}
    except Exception as e:
        logger.warning("ensure_surprise_schema failed: %s", e)
        return {"ok": False, "error": str(e)[:240]}
    finally:
        if eng is not None:
            try:
                eng.dispose()
            except Exception:
                pass