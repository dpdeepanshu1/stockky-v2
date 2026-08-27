"""
db.py — engine/session setup for real-trade-service.

Decision 3: this service does NOT get its own database. It points at the
exact same Oracle Autonomous DB (or Neon/Postgres in local/Render dev) as
every other Stockky service, via the identical ORACLE_* / DATABASE_URL env
contract oracle_compat.py already defines. Only the tables in models.py are
new — same instance, new schema objects, all prefixed trade_ so they can
never collide with existing tables.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import oracle_compat as _oc

logger = logging.getLogger("real-trade-db")

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
    """'oracle' on the Oracle Cloud VM deploy, 'postgresql' on Render/Neon —
    exactly mirrors every other Stockky service's detection rule."""
    if _oc.oracle_is_configured(config.DATABASE_URL):
        return "oracle"
    return "postgresql"


def get_engine():
    """Lazily build (once) and return the SQLAlchemy engine for whichever
    backend is configured. Returns None if neither Oracle nor a Postgres
    DATABASE_URL is configured — callers must handle that (fail loud at
    startup, not silently no-op on every DB call)."""
    global _engine
    if _engine is not None:
        return _engine

    if _oc.oracle_is_configured(config.DATABASE_URL):
        _engine, _ = _oc.build_oracle_engine(
            config.DATABASE_URL,
            db_pool_size=2,      # this service's write volume is low —
            db_max_overflow=2,   # order/position events, not scan traffic
        )
        return _engine

    url = config.DATABASE_URL
    if not url:
        logger.warning("No DATABASE_URL/ORACLE_DSN configured — real-trade-service has no DB.")
        return None
    url = _normalize_pg_url(url)
    _engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=2,
        pool_timeout=10,
        connect_args={"connect_timeout": 10, "application_name": "real-trade-service"},
    )
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        eng = get_engine()
        if eng is None:
            raise RuntimeError("real-trade-service: no database configured (set DATABASE_URL or ORACLE_DSN).")
        _SessionLocal = sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)
    return _SessionLocal


def get_db():
    """FastAPI dependency — yields a Session, always closed after the request."""
    Session = get_session_factory()
    db = Session()
    try:
        yield db
    finally:
        db.close()


def init_schema() -> None:
    """Create every trade_* table if it doesn't already exist. Safe to call
    on every boot (each service instance does this once at startup) —
    CREATE TABLE IF NOT EXISTS on Postgres, and the Oracle branch swallows
    ORA-00955 'name already used' the same way oracle_compat.py's
    exec_ddl_safe does elsewhere in this codebase."""
    import models  # local import: avoids a circular import at module load

    eng = get_engine()
    if eng is None:
        raise RuntimeError("real-trade-service: cannot init schema, no database configured.")
    models.Base.metadata.create_all(eng, checkfirst=True)
    logger.info("real-trade-service: schema ready (dialect=%s)", dialect())

    if dialect() == "oracle":
        _ensure_oracle_autoincrement(eng, models.Base)

    _ensure_manual_order_columns(eng, dialect())
    _ensure_gate_state_columns(eng, dialect())


# create_all(checkfirst=True) only creates MISSING TABLES — it never adds a
# column to a table that already exists (see SQLAlchemy docs: it diffs
# table names, not column sets). trade_orders existed before
# execution_source/confirmed_by/confirmed_at were added to models.py, so on
# any already-deployed DB those three columns must be added by hand, once,
# additively — same idiom decision-prediction-service/training/models.py
# already uses for its own schema drift. Safe to call on every boot: each
# ALTER is wrapped so "column already exists" (Postgres) / ORA-01430
# (Oracle) is swallowed exactly like exec_ddl_safe does for "table already
# exists" elsewhere in this file.
def _ensure_manual_order_columns(engine, dialect_name: str) -> None:
    from sqlalchemy import inspect, text

    try:
        existing = {c["name"] for c in inspect(engine).get_columns("trade_orders")}
    except Exception as e:
        logger.warning("real-trade-db: could not inspect trade_orders columns: %s", e)
        return

    if dialect_name == "oracle":
        adds = [
            ("execution_source", "ALTER TABLE trade_orders ADD (execution_source VARCHAR2(16) DEFAULT 'AUTO' NOT NULL)"),
            ("confirmed_by", "ALTER TABLE trade_orders ADD (confirmed_by VARCHAR2(64))"),
            ("confirmed_at", "ALTER TABLE trade_orders ADD (confirmed_at TIMESTAMP)"),
            ("exit_reason", "ALTER TABLE trade_orders ADD (exit_reason VARCHAR2(32))"),
        ]
    else:
        adds = [
            ("execution_source", "ALTER TABLE trade_orders ADD COLUMN execution_source VARCHAR(16) DEFAULT 'AUTO' NOT NULL"),
            ("confirmed_by", "ALTER TABLE trade_orders ADD COLUMN confirmed_by VARCHAR(64)"),
            ("confirmed_at", "ALTER TABLE trade_orders ADD COLUMN confirmed_at TIMESTAMP"),
            ("exit_reason", "ALTER TABLE trade_orders ADD COLUMN exit_reason VARCHAR(32)"),
        ]

    for col_name, sql in adds:
        if col_name in existing:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
            logger.info("real-trade-db: added trade_orders.%s", col_name)
        except Exception as e:
            m = str(e)
            if "already exists" in m.lower() or "ORA-01430" in m:
                continue
            logger.warning("real-trade-db: could not add trade_orders.%s: %s", col_name, e)


# Same additive-migration idiom as _ensure_manual_order_columns above —
# trade_gate_state existed before auto_pilot_enabled/auto_pilot_enabled_at
# were added to models.py (2026-08-27, Auto-Pilot feature), so on any
# already-deployed DB these two columns must be added by hand, once.
def _ensure_gate_state_columns(engine, dialect_name: str) -> None:
    from sqlalchemy import inspect, text

    try:
        existing = {c["name"] for c in inspect(engine).get_columns("trade_gate_state")}
    except Exception as e:
        logger.warning("real-trade-db: could not inspect trade_gate_state columns: %s", e)
        return

    if dialect_name == "oracle":
        adds = [
            ("auto_pilot_enabled", "ALTER TABLE trade_gate_state ADD (auto_pilot_enabled NUMBER(1) DEFAULT 0 NOT NULL)"),
            ("auto_pilot_enabled_at", "ALTER TABLE trade_gate_state ADD (auto_pilot_enabled_at TIMESTAMP)"),
        ]
    else:
        adds = [
            ("auto_pilot_enabled", "ALTER TABLE trade_gate_state ADD COLUMN auto_pilot_enabled BOOLEAN DEFAULT FALSE NOT NULL"),
            ("auto_pilot_enabled_at", "ALTER TABLE trade_gate_state ADD COLUMN auto_pilot_enabled_at TIMESTAMP"),
        ]

    for col_name, sql in adds:
        if col_name in existing:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
            logger.info("real-trade-db: added trade_gate_state.%s", col_name)
        except Exception as e:
            m = str(e)
            if "already exists" in m.lower() or "ORA-01430" in m:
                continue
            logger.warning("real-trade-db: could not add trade_gate_state.%s: %s", col_name, e)


# Every trade_* model uses `id = Column(Integer, primary_key=True,
# autoincrement=True)`. On Postgres that's always backed by a real serial/
# identity sequence. On Oracle, SQLAlchemy's create_all() only emits a
# GENERATED ... AS IDENTITY clause the FIRST time it creates a table — if a
# trade_* table already existed from an earlier deploy (e.g. created by an
# older SQLAlchemy version, or before this service had any tables to diff
# against), checkfirst=True sees the table already exists and never adds an
# identity/sequence to it. Every subsequent INSERT then sends id=NULL and
# Oracle rejects it with ORA-01400 ("cannot insert NULL into ID") — this is
# exactly the crash-loop seen in the 26/8 deploy logs, starting with the
# very first _seed_defaults() insert into trade_accounts.
#
# Fix: for every trade_* table, if it has no identity column on `id`,
# attach a sequence + BEFORE INSERT trigger that fills `id` from the
# sequence whenever a row arrives with id IS NULL. This is idempotent,
# additive (never touches existing data), and works whether or not the
# table has a "real" IDENTITY column — the trigger only fires on the null
# case, so a table that DOES already have working identity is unaffected.
def _ensure_oracle_autoincrement(engine, base) -> None:
    from sqlalchemy import text

    tables = [t.name for t in base.metadata.sorted_tables]
    with engine.connect() as conn:
        for table in tables:
            try:
                has_identity = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM user_tab_identity_cols "
                        "WHERE table_name = UPPER(:t) AND column_name = 'ID'"
                    ),
                    {"t": table},
                ).scalar()
            except Exception as e:
                logger.warning("oracle identity check failed for %s: %s", table, e)
                continue
            if has_identity:
                continue  # real IDENTITY column already present — nothing to do

            seq_name = f"{table}_id_seq"
            trg_name = f"trg_{table}_bi"
            try:
                start_at = conn.execute(
                    text(f"SELECT NVL(MAX(id), 0) + 1 FROM {table}")  # noqa: S608 - table name from trusted metadata
                ).scalar() or 1
            except Exception:
                start_at = 1

            _oc.exec_ddl_safe(
                engine,
                f"CREATE SEQUENCE {seq_name} START WITH {int(start_at)} "
                f"INCREMENT BY 1 NOCACHE NOCYCLE",
                "oracle",
            )
            try:
                with engine.begin() as trg_conn:
                    # exec_driver_sql — NOT text() — is required here: this
                    # DDL contains literal Oracle trigger correlation syntax
                    # (:NEW.id) that SQLAlchemy's text() would otherwise
                    # misparse as ITS OWN bind parameter named "NEW" and
                    # then fail with "a value is required for bind
                    # parameter 'NEW'" (exactly the warning that showed up
                    # in production logs — the trigger was silently never
                    # created on any table, so the ORA-01400 crash kept
                    # happening even after this fix first shipped).
                    # exec_driver_sql sends the string straight to the
                    # oracledb driver with no SQLAlchemy-side parameter
                    # parsing at all, so :NEW.id reaches Oracle untouched.
                    trg_conn.exec_driver_sql(
                        f"CREATE OR REPLACE TRIGGER {trg_name} "
                        f"BEFORE INSERT ON {table} FOR EACH ROW "
                        f"WHEN (NEW.id IS NULL) "
                        f"BEGIN SELECT {seq_name}.NEXTVAL INTO :NEW.id FROM dual; END;"
                    )
                logger.info(
                    "real-trade-db: attached %s / %s to %s (backfill autoincrement)",
                    seq_name, trg_name, table,
                )
            except Exception as e:
                logger.warning("real-trade-db: could not attach autoincrement trigger to %s: %s", table, e)
