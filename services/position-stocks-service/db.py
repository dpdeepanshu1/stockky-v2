"""
db.py — engine/session setup for position-stocks-service.

Same DB instance as every other Stockky service (same ORACLE_* /
DATABASE_URL env contract oracle_compat.py defines — copied verbatim from
real-trade-service, unmodified). Only the tables in models.py are new, all
prefixed `scalp_` so they can never collide with the existing `trade_*`
tables real-trade-service owns.

Sharing the physical DB is a data-layer choice, not a process-coupling
one: this service has its own connection pool, its own engine, its own
session factory — a slow query here cannot block real-trade-service's
connections or vice versa, they're independent pools against the same
server.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import oracle_compat as _oc

logger = logging.getLogger("position-stocks-db")

_engine = None
_SessionLocal = None


def _normalize_pg_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if "channel_binding=" in url:
        url = re.sub(r"([&?])channel_binding=[^&]*", r"\1", url)
        url = url.replace("?&", "?").rstrip("?&")
    if "sslmode=" not in url.lower():
        url = url + ("&" if "?" in url else "?") + "sslmode=require"
    return url


def dialect() -> str:
    if _oc.oracle_is_configured(config.DATABASE_URL):
        return "oracle"
    return "postgresql"


def get_engine():
    global _engine
    if _engine is not None:
        return _engine

    if _oc.oracle_is_configured(config.DATABASE_URL):
        _engine, _ = _oc.build_oracle_engine(
            config.DATABASE_URL,
            db_pool_size=config.DB_POOL_SIZE,
            db_max_overflow=config.DB_MAX_OVERFLOW,
        )
        logger.info("position-stocks-service: connected via Oracle Autonomous DB")
    elif config.DATABASE_URL:
        url = _normalize_pg_url(config.DATABASE_URL)
        _engine = create_engine(
            url,
            pool_size=config.DB_POOL_SIZE,
            max_overflow=config.DB_MAX_OVERFLOW,
            pool_recycle=config.DB_POOL_RECYCLE,
            pool_timeout=config.DB_POOL_TIMEOUT,
            pool_pre_ping=True,
        )
        logger.info("position-stocks-service: connected via Postgres/Neon")
    else:
        logger.error(
            "position-stocks-service: no DATABASE_URL or ORACLE_DSN configured — "
            "this service cannot persist anything (ledger/positions/candidates). "
            "Fix the env before arming."
        )
        return None
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is not None:
        return _SessionLocal
    engine = get_engine()
    if engine is None:
        return None
    _SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return _SessionLocal


def get_db():
    """FastAPI dependency — yields a Session, always closes it."""
    factory = get_session_factory()
    if factory is None:
        raise RuntimeError("Database not configured — see db.get_engine() log line.")
    db = factory()
    try:
        yield db
    finally:
        db.close()


def init_tables() -> None:
    """Create scalp_* tables if they don't exist. Never touches trade_* tables
    (this service never writes models for those — see models.py).

    Also runs _ensure_columns() after create_all(): SQLAlchemy's create_all()
    only creates missing TABLES, it never ALTERs an existing one — so a table
    created by an older version of models.py (e.g. scalp_gate_state before
    service_enabled existed) would otherwise need a manual ALTER TABLE before
    every deploy that adds a column. _ensure_columns() makes that automatic
    and idempotent instead (2026-09-12, session 4)."""
    import models  # local import: avoids circular import at module load time
    engine = get_engine()
    if engine is None:
        logger.error("init_tables: no engine — skipping (DB not configured).")
        return
    models.Base.metadata.create_all(engine)
    _ensure_columns(engine)
    logger.info("position-stocks-service: scalp_* tables ensured.")


# Columns added to models.py after a table may already have been created in a
# live DB. Each entry: (table, column, Oracle DDL type, Postgres DDL type,
# default SQL literal). Add a new tuple here whenever a column is added to an
# existing model — never remove old entries, they're harmless no-ops once
# applied everywhere.
_COLUMN_MIGRATIONS = [
    ("scalp_gate_state", "service_enabled", "NUMBER(1)", "BOOLEAN", "1", "TRUE"),
]


def _ensure_columns(engine) -> None:
    """Idempotent: for each (table, column) in _COLUMN_MIGRATIONS, check via
    SQLAlchemy's inspector whether the column already exists on the live
    table; if not, ALTER TABLE ... ADD COLUMN with a NOT NULL default so
    existing rows get a sane value. Safe to run on every boot — a no-op once
    the column exists everywhere. Never touches trade_* tables."""
    from sqlalchemy import inspect, text

    is_oracle = dialect() == "oracle"
    inspector = inspect(engine)
    for table, column, oracle_type, pg_type, oracle_default, pg_default in _COLUMN_MIGRATIONS:
        try:
            if not inspector.has_table(table):
                # Table doesn't exist yet at all — create_all() will make it
                # with the column already present next time it's called on a
                # fresh table; nothing to migrate.
                continue
            existing_cols = {c["name"].lower() for c in inspector.get_columns(table)}
            if column.lower() in existing_cols:
                continue

            if is_oracle:
                ddl = f"ALTER TABLE {table} ADD {column} {oracle_type} DEFAULT {oracle_default} NOT NULL"
            else:
                ddl = f"ALTER TABLE {table} ADD COLUMN {column} {pg_type} DEFAULT {pg_default} NOT NULL"

            with engine.begin() as conn:
                conn.execute(text(ddl))
            logger.warning(
                "position-stocks-service: migrated — added missing column "
                "%s.%s (table pre-dated this field).", table, column,
            )
        except Exception as e:
            # Never let a migration failure crash startup — log loudly and
            # continue; worst case the column is still missing and whatever
            # queries it will surface that clearly, rather than the whole
            # service failing to boot over a DDL edge case.
            logger.error(
                "position-stocks-service: _ensure_columns failed for %s.%s: %s",
                table, column, e, exc_info=True,
            )
