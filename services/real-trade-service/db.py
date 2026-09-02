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

    # BUG FIX (31-Aug-2026): pool_size=2/max_overflow=2 (4 connections total)
    # was sized for "write volume is low — order/position events, not scan
    # traffic", which undercounted this service's actual concurrent DB
    # users once Auto-Pilot shipped: the fast-exit loop and the full-cycle
    # loop are two INDEPENDENT asyncio background tasks (execution/
    # auto_pilot.py) that can each hold a checked-out connection at the
    # same time, and the full cycle in particular (candidates → entry →
    # fills → expire → exit → reconcile, see cycle_runner.py) holds its
    # single connection open for the connection's ENTIRE duration —
    # including slow/failing outbound candidate-scan HTTP calls (the
    # WriteTimeout errors seen alongside this in the logs). Add normal
    # dashboard traffic on top (/status/DEMO + /status/REAL polling,
    # /auth/login, manual /cycle/run) and 4 total connections was routinely
    # exhausted, producing the "QueuePool limit ... connection timed out"
    # crash loop on every request (gate_status, login, exit-only tick)
    # while a full cycle was mid-flight. Raised to 4/4 (8 total) — still
    # modest for a service with no write-heavy hot path, but enough
    # headroom for 2 background loops + a few concurrent dashboard/API
    # requests without queuing past pool_timeout.
    if _oc.oracle_is_configured(config.DATABASE_URL):
        _engine, _ = _oc.build_oracle_engine(
            config.DATABASE_URL,
            db_pool_size=4,
            db_max_overflow=4,
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
        pool_size=4,
        max_overflow=4,
        pool_timeout=30,
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
    _ensure_position_columns(eng, dialect())
    _ensure_watchlist_link_columns(eng, dialect())
    _fix_stale_dhan_token_expiry(eng)


# 2026-08-27 data fixup: docker-compose.yml/.env.example/.env.oracle.example
# used to override DHAN_TOKEN_LIFETIME_DAYS to 30 even though Dhan hard-caps
# every access token at 24h (see CHANGES_2026-08-27_REVIEW.md #2). Any
# trade_credentials row saved while that misconfiguration was live has a
# token_expires_at up to ~29 days past what Dhan will actually honor —
# auth/dhan_credentials.py now clamps this defensively on every read, but
# fixing the stored value too means the dashboard, DB, and any other
# consumer all agree instead of relying on every caller to remember to
# clamp. Idempotent — once a row is capped it stays capped since capping
# again is a no-op comparison, not an unconditional overwrite.
def _fix_stale_dhan_token_expiry(engine) -> None:
    from sqlalchemy import text

    try:
        with engine.begin() as conn:
            result = conn.execute(text(
                "UPDATE trade_credentials "
                "SET token_expires_at = token_issued_at + INTERVAL '24 hours' "
                "WHERE token_issued_at IS NOT NULL "
                "AND token_expires_at IS NOT NULL "
                "AND token_expires_at > token_issued_at + INTERVAL '24 hours'"
            ) if dialect() != "oracle" else text(
                "UPDATE trade_credentials "
                "SET token_expires_at = token_issued_at + INTERVAL '24' HOUR "
                "WHERE token_issued_at IS NOT NULL "
                "AND token_expires_at IS NOT NULL "
                "AND token_expires_at > token_issued_at + INTERVAL '24' HOUR"
            ))
            if result.rowcount:
                logger.info("real-trade-db: capped %s stale trade_credentials.token_expires_at row(s) to 24h", result.rowcount)
    except Exception as e:
        logger.warning("real-trade-db: could not check/fix stale token_expires_at: %s", e)


# create_all(checkfirst=True) only creates MISSING TABLES — it never adds a
# column to a table that already exists (see SQLAlchemy docs: it diffs
# table names, not column sets). trade_orders existed before
# execution_source/confirmed_by/confirmed_at/filled_qty_so_far were added to
# models.py, so on
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
            ("filled_qty_so_far", "ALTER TABLE trade_orders ADD (filled_qty_so_far NUMBER(10) DEFAULT 0 NOT NULL)"),
        ]
    else:
        adds = [
            ("execution_source", "ALTER TABLE trade_orders ADD COLUMN execution_source VARCHAR(16) DEFAULT 'AUTO' NOT NULL"),
            ("confirmed_by", "ALTER TABLE trade_orders ADD COLUMN confirmed_by VARCHAR(64)"),
            ("confirmed_at", "ALTER TABLE trade_orders ADD COLUMN confirmed_at TIMESTAMP"),
            ("exit_reason", "ALTER TABLE trade_orders ADD COLUMN exit_reason VARCHAR(32)"),
            ("filled_qty_so_far", "ALTER TABLE trade_orders ADD COLUMN filled_qty_so_far INTEGER DEFAULT 0 NOT NULL"),
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
            ("prepick_enabled", "ALTER TABLE trade_gate_state ADD (prepick_enabled NUMBER(1) DEFAULT 0 NOT NULL)"),
            ("prepick_enabled_at", "ALTER TABLE trade_gate_state ADD (prepick_enabled_at TIMESTAMP)"),
            ("prepick_last_run", "ALTER TABLE trade_gate_state ADD (prepick_last_run VARCHAR2(10))"),
            ("enter_at_open_enabled", "ALTER TABLE trade_gate_state ADD (enter_at_open_enabled NUMBER(1) DEFAULT 0 NOT NULL)"),
            ("enter_at_open_enabled_at", "ALTER TABLE trade_gate_state ADD (enter_at_open_enabled_at TIMESTAMP)"),
            ("enter_at_open_last_run", "ALTER TABLE trade_gate_state ADD (enter_at_open_last_run VARCHAR2(10))"),
            ("eod_squareoff_enabled", "ALTER TABLE trade_gate_state ADD (eod_squareoff_enabled NUMBER(1) DEFAULT 0 NOT NULL)"),
            ("eod_squareoff_enabled_at", "ALTER TABLE trade_gate_state ADD (eod_squareoff_enabled_at TIMESTAMP)"),
            ("eod_squareoff_last_run", "ALTER TABLE trade_gate_state ADD (eod_squareoff_last_run VARCHAR2(10))"),
        ]
    else:
        adds = [
            ("auto_pilot_enabled", "ALTER TABLE trade_gate_state ADD COLUMN auto_pilot_enabled BOOLEAN DEFAULT FALSE NOT NULL"),
            ("auto_pilot_enabled_at", "ALTER TABLE trade_gate_state ADD COLUMN auto_pilot_enabled_at TIMESTAMP"),
            ("prepick_enabled", "ALTER TABLE trade_gate_state ADD COLUMN prepick_enabled BOOLEAN DEFAULT FALSE NOT NULL"),
            ("prepick_enabled_at", "ALTER TABLE trade_gate_state ADD COLUMN prepick_enabled_at TIMESTAMP"),
            ("prepick_last_run", "ALTER TABLE trade_gate_state ADD COLUMN prepick_last_run VARCHAR(10)"),
            ("enter_at_open_enabled", "ALTER TABLE trade_gate_state ADD COLUMN enter_at_open_enabled BOOLEAN DEFAULT FALSE NOT NULL"),
            ("enter_at_open_enabled_at", "ALTER TABLE trade_gate_state ADD COLUMN enter_at_open_enabled_at TIMESTAMP"),
            ("enter_at_open_last_run", "ALTER TABLE trade_gate_state ADD COLUMN enter_at_open_last_run VARCHAR(10)"),
            ("eod_squareoff_enabled", "ALTER TABLE trade_gate_state ADD COLUMN eod_squareoff_enabled BOOLEAN DEFAULT FALSE NOT NULL"),
            ("eod_squareoff_enabled_at", "ALTER TABLE trade_gate_state ADD COLUMN eod_squareoff_enabled_at TIMESTAMP"),
            ("eod_squareoff_last_run", "ALTER TABLE trade_gate_state ADD COLUMN eod_squareoff_last_run VARCHAR(10)"),
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
# Same additive-migration idiom as _ensure_manual_order_columns above —
# trade_positions existed before initial_stop_distance was added to
# models.py (2026-09-01, gap-down emergency-exit fix), so on any
# already-deployed DB this column must be added by hand, once. Nullable
# and left NULL for existing open rows — exit_engine falls back to its
# previous current_stop-based approximation for those until they close.
def _ensure_position_columns(engine, dialect_name: str) -> None:
    from sqlalchemy import inspect, text

    try:
        existing = {c["name"] for c in inspect(engine).get_columns("trade_positions")}
    except Exception as e:
        logger.warning("real-trade-db: could not inspect trade_positions columns: %s", e)
        return

    if dialect_name == "oracle":
        adds = [
            ("initial_stop_distance", "ALTER TABLE trade_positions ADD (initial_stop_distance FLOAT)"),
        ]
    else:
        adds = [
            ("initial_stop_distance", "ALTER TABLE trade_positions ADD COLUMN initial_stop_distance FLOAT"),
        ]

    for col_name, sql in adds:
        if col_name in existing:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
            logger.info("real-trade-db: added trade_positions.%s", col_name)
        except Exception as e:
            m = str(e)
            if "already exists" in m.lower() or "ORA-01430" in m:
                continue
            logger.warning("real-trade-db: could not add trade_positions.%s: %s", col_name, e)


# Short-Term Trading Upgrade (2026-09-02): trade_candidates, trade_orders,
# and trade_positions all existed before watchlist_entry_id was added to
# models.py — same additive-migration idiom as every _ensure_* fn above.
# trade_watchlist/trade_resilience_cache are brand-new tables so create_all()
# handles them; only the FK-carrying columns on pre-existing tables need this.
# All three are nullable and default NULL, so every pre-existing row and every
# future non-watchlist row is completely unaffected.
def _ensure_watchlist_link_columns(engine, dialect_name: str) -> None:
    from sqlalchemy import inspect, text

    targets = [
        ("trade_candidates", "watchlist_entry_id"),
        ("trade_orders",     "watchlist_entry_id"),
        ("trade_positions",  "watchlist_entry_id"),
    ]
    for table_name, col_name in targets:
        try:
            existing = {c["name"] for c in inspect(engine).get_columns(table_name)}
        except Exception as e:
            logger.warning("real-trade-db: could not inspect %s columns: %s", table_name, e)
            continue
        if col_name in existing:
            continue
        if dialect_name == "oracle":
            sql = f"ALTER TABLE {table_name} ADD ({col_name} NUMBER(10))"
        else:
            sql = f"ALTER TABLE {table_name} ADD COLUMN {col_name} INTEGER"
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
            logger.info("real-trade-db: added %s.%s", table_name, col_name)
        except Exception as e:
            m = str(e)
            if "already exists" in m.lower() or "ORA-01430" in m:
                continue
            logger.warning("real-trade-db: could not add %s.%s: %s", table_name, col_name, e)


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
