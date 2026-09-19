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
    and idempotent instead (2026-09-12, session 4).

    Also runs _ensure_oracle_autoincrement() on Oracle: SQLAlchemy's
    create_all() emits GENERATED AS IDENTITY on a brand-new table, but if the
    table was created by an older deploy (or by a manual CREATE TABLE without
    the clause), subsequent INSERTs send id=NULL and Oracle raises ORA-01400.
    The fix mirrors real-trade-service's db.py — a SEQUENCE + BEFORE INSERT
    TRIGGER is attached for any scalp_* table whose `id` column lacks an
    Oracle IDENTITY column (2026-09-13, session 8)."""
    import models  # local import: avoids circular import at module load time
    engine = get_engine()
    if engine is None:
        logger.error("init_tables: no engine — skipping (DB not configured).")
        return
    models.Base.metadata.create_all(engine)
    _ensure_columns(engine)
    if dialect() == "oracle":
        _ensure_oracle_autoincrement(engine, models.Base)
    _ensure_hot_path_indexes(engine, dialect())
    logger.info("position-stocks-service: scalp_* tables ensured.")


# Columns added to models.py after a table may already have been created in a
# live DB. Each entry: (table, column, Oracle DDL type, Postgres DDL type,
# oracle_default_or_None, pg_default_or_None). When both defaults are None,
# the column is added as nullable with no default (for optional/nullable
# fields like audit data) instead of NOT NULL DEFAULT ... Add a new tuple
# here whenever a column is added to an existing model — never remove old
# entries, they're harmless no-ops once applied everywhere.
_COLUMN_MIGRATIONS = [
    ("scalp_gate_state", "service_enabled", "NUMBER(1)", "BOOLEAN", "1", "TRUE"),
    ("scalp_gate_state", "auto_pilot_enabled", "NUMBER(1)", "BOOLEAN", "1", "TRUE"),
    ("scalp_gate_state", "last_cycle_run_at", "TIMESTAMP", "TIMESTAMP", None, None),
    ("scalp_gate_state", "last_cycle_run_trigger", "VARCHAR2(16)", "VARCHAR(16)", None, None),
    # AUDIT FIX (this session): daily_loss_kill_switch_tripped/_date,
    # orders_placed_today/_date, and first_live_order_done are all declared
    # on ScalpGateState (models.py) and actively read/written throughout
    # main.py and orders/entry.py (order-budget guard, first-live-order
    # safety valve, kill-switch checks) — but were never added here. Any
    # scalp_gate_state table created before these fields existed on the
    # model is missing the actual DB columns, so every SELECT/UPDATE that
    # touches a ScalpGateState row (i.e. almost every request this service
    # handles) fails at the DB level, not merely with a Python
    # AttributeError. Same bug class already fixed for scalp_capital_ledger
    # below (session 9) — that fix only covered one of the two tables that
    # needed it.
    ("scalp_gate_state", "daily_loss_kill_switch_tripped", "NUMBER(1)", "BOOLEAN", "0", "FALSE"),
    ("scalp_gate_state", "daily_loss_kill_switch_tripped_date", "VARCHAR2(10)", "VARCHAR(10)", None, None),
    ("scalp_gate_state", "orders_placed_today", "NUMBER(10)", "INTEGER", "0", "0"),
    ("scalp_gate_state", "orders_placed_today_date", "VARCHAR2(10)", "VARCHAR(10)", None, None),
    ("scalp_gate_state", "first_live_order_done", "NUMBER(1)", "BOOLEAN", "0", "FALSE"),
    # session69: stagnation_exit_enabled added to ScalpGateState so the
    # existing production table (created before this column existed) picks
    # it up on next boot — same idempotent ALTER TABLE pattern as every
    # other scalp_gate_state column above.
    ("scalp_gate_state", "stagnation_exit_enabled", "NUMBER(1)", "BOOLEAN", "0", "FALSE"),
    # this session: retention_cleanup_last_run_date added to ScalpGateState
    # (see models.py) — same idempotent ALTER TABLE pattern as every other
    # scalp_gate_state column above, needed so an already-deployed table
    # picks it up on next boot.
    ("scalp_gate_state", "retention_cleanup_last_run_date", "VARCHAR2(10)", "VARCHAR(10)", None, None),
    # this session: breakeven_stop_enabled added to ScalpGateState (see
    # models.py comment) — same idempotent ALTER TABLE pattern as
    # stagnation_exit_enabled above.
    ("scalp_gate_state", "breakeven_stop_enabled", "NUMBER(1)", "BOOLEAN", "0", "FALSE"),
    # 2026-09-19 (audit finding): edis_check_last_run_date added to
    # ScalpGateState — see that column's comment in models.py.
    ("scalp_gate_state", "edis_check_last_run_date", "VARCHAR2(10)", "VARCHAR(10)", None, None),
    # this session: breakeven_trigger_pct/stop_moved_to_breakeven added to
    # ScalpPosition (see models.py comment). No prior scalp_positions
    # migration tuples exist in this list because every earlier column
    # added to that table happened to land before any production deploy of
    # this table existed yet — these two are the first to need one on an
    # already-deployed table, so they're added here rather than assumed to
    # arrive via create_all() alone.
    ("scalp_positions", "breakeven_trigger_pct", "BINARY_DOUBLE", "DOUBLE PRECISION", None, None),
    ("scalp_positions", "stop_moved_to_breakeven", "NUMBER(1)", "BOOLEAN", "0", "FALSE"),
    # 2026-09-18 (user audit finding): marks a carried-overnight position
    # that was successfully converted INTRADAY -> CNC via
    # execution/dhan_client.py::convert_position — see the
    # OVERNIGHT_HOLD_ENABLED comment block in config.py for why this
    # conversion is required at all. NULL/False for every normal
    # same-day position; only ever set True by orders/eod_squareoff.py's
    # carry path.
    ("scalp_positions", "overnight_converted_to_cnc", "NUMBER(1)", "BOOLEAN", "0", "FALSE"),
    # 2026-09-19 (option 3 fix): Dhan order_id of the STOP_LOSS_MARKET SELL
    # placed immediately after INTRADAY -> CNC conversion (see models.py
    # comment on overnight_stop_order_id and config.py's
    # OVERNIGHT_STOP_LOSS_PCT).  Nullable — NULL means no protective stop
    # order is currently live (either not yet placed, already triggered, or
    # placement failed and the position was squared off instead).
    ("scalp_positions", "overnight_stop_order_id", "VARCHAR2(64)", "VARCHAR(64)", None, None),
    # 2026-09-19 (audit fix — overnight-stop partial-fill handling): see
    # the column's comment in models.py. NOT NULL default 0, same idempotent
    # ALTER TABLE pattern as every other scalp_positions column above.
    ("scalp_positions", "overnight_stop_filled_qty_so_far", "NUMBER(10)", "INTEGER", "0", "0"),
    # 2026-09-19 (session 72): see models.py — per-order notional + lifetime prior qty.
    ("scalp_positions", "overnight_stop_filled_notional_so_far", "BINARY_DOUBLE", "DOUBLE PRECISION", "0", "0"),
    ("scalp_positions", "overnight_stop_prior_qty", "NUMBER(10)", "INTEGER", "0", "0"),
    ("scalp_candidate_log", "fundamental_score", "BINARY_DOUBLE", "DOUBLE PRECISION", None, None),
    ("scalp_candidate_log", "technical_score", "BINARY_DOUBLE", "DOUBLE PRECISION", None, None),
    ("scalp_candidate_log", "market_cap_cr", "BINARY_DOUBLE", "DOUBLE PRECISION", None, None),
    ("scalp_candidate_log", "has_positive_catalyst", "NUMBER(1)", "BOOLEAN", None, None),
    # BUG FIX (2026-09-13, session 9): scalp_capital_ledger was created before
    # daily_loss_kill_switch_tripped / daily_loss_kill_switch_tripped_date were
    # added to the model. The row exists (inserted on first boot) but lacks
    # these columns, so every access to row.daily_loss_kill_switch_tripped raised
    # AttributeError -> 500 on GET /ledger. Adding them here lets _ensure_columns()
    # ALTER TABLE idempotently on the next boot, same pattern as gate_state above.
    ("scalp_capital_ledger", "daily_loss_kill_switch_tripped", "NUMBER(1)", "BOOLEAN", "0", "FALSE"),
    ("scalp_capital_ledger", "daily_loss_kill_switch_tripped_date", "VARCHAR2(10)", "VARCHAR(10)", None, None),
    # BUG FIX (session13 audit): backs capital/ledger.py's lazy daily reset
    # (reset_daily() was previously dead code, never called — see models.py
    # comment on this column for the full story).
    ("scalp_capital_ledger", "pnl_last_reset_date", "VARCHAR2(10)", "VARCHAR(10)", None, None),
    # BUG FIX (Issue #2): cross-service PnL sync columns added to
    # scalp_capital_ledger so reserve_capital()'s daily-loss kill switch
    # accounts for losses booked by real-trade-service on the same Dhan account.
    ("scalp_capital_ledger", "peer_realized_pnl_today", "BINARY_DOUBLE", "DOUBLE PRECISION", "0", "0"),
    ("scalp_capital_ledger", "peer_pnl_last_synced_at", "TIMESTAMP", "TIMESTAMP", None, None),
    # AUDIT FIX (2026-09-15): scalp_intraday_restricted is a brand-new table
    # (models.ScalpIntradayRestrictedSecurity). create_all() will CREATE it
    # on the next boot — no column migrations needed for a new table.
    # symbol is the PK (not an `id` column), so _ensure_oracle_autoincrement()
    # skips it automatically — correct. Add column entries here if new
    # columns are ever added to that table on a running deployment.
]


def _ensure_columns(engine) -> None:
    """Idempotent: for each (table, column) in _COLUMN_MIGRATIONS, check via
    SQLAlchemy's inspector whether the column already exists on the live
    table; if not, ALTER TABLE ... ADD COLUMN. When a default is given, adds
    it NOT NULL with that default so existing rows get a sane value; when
    both defaults are None, adds it as a plain nullable column (for optional
    audit-style fields where NULL correctly means "unknown"/"not recorded
    yet"). Safe to run on every boot — a no-op once the column exists
    everywhere. Never touches trade_* tables."""
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

            nullable = oracle_default is None and pg_default is None
            if is_oracle:
                ddl = f"ALTER TABLE {table} ADD {column} {oracle_type}"
                if not nullable:
                    ddl += f" DEFAULT {oracle_default} NOT NULL"
            else:
                ddl = f"ALTER TABLE {table} ADD COLUMN {column} {pg_type}"
                if not nullable:
                    ddl += f" DEFAULT {pg_default} NOT NULL"

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


# SESSION 33 AUDIT FIX (index/performance): same create_all() limitation as
# _ensure_columns above — it only ever adds an index to a table it is ALSO
# creating for the first time, never retrofits one onto a table that already
# exists. scalp_positions predates this index. GET /positions and
# GET /trades/history (main.py) both run `order_by(ScalpPosition.
# opened_at.desc())` on every dashboard poll — /trades/history additionally
# filters by status when status_filter is given, but the unfiltered
# order-by-opened_at scan is the one that runs on every plain poll and had
# zero index support. Uses the same create_index_sql/exec_ddl_safe idiom
# api-gateway's hotpicks_schema.py already uses: CREATE INDEX IF NOT EXISTS
# on Postgres, plain CREATE INDEX on Oracle with ORA-00955/ORA-01408
# swallowed by exec_ddl_safe on a re-run.
def _ensure_hot_path_indexes(engine, dialect_name: str) -> None:
    index_name, table, cols = "ix_scalp_positions_opened_at", "scalp_positions", "opened_at"
    sql = _oc.create_index_sql(dialect_name, index_name, table, cols)
    _oc.exec_ddl_safe(engine, sql, dialect_name)
    logger.info("position-stocks-service: ensured index %s on %s", index_name, table)


def _ensure_oracle_autoincrement(engine, base) -> None:
    """BUG FIX (2026-09-13, session 8): ORA-01400 'cannot insert NULL into ID'.

    On Oracle, SQLAlchemy's create_all() only emits GENERATED AS IDENTITY the
    FIRST time it creates a table. If the scalp_* tables were created by an
    older deploy (or manually, without the IDENTITY clause), every INSERT
    thereafter sends id=NULL and Oracle raises ORA-01400. Fix: for every
    scalp_* table with an `id` PK, if it has no IDENTITY column yet, create a
    SEQUENCE + BEFORE INSERT TRIGGER that populates :NEW.id when it is NULL.
    Idempotent: a table that already has a working IDENTITY or trigger is left
    untouched. Mirrors real-trade-service/db.py's _ensure_oracle_autoincrement
    verbatim (same isolation rationale — duplicated, not imported)."""
    from sqlalchemy import text

    tables_with_id_pk = {
        t.name
        for t in base.metadata.sorted_tables
        if "id" in {c.name for c in t.primary_key.columns}
    }
    with engine.connect() as conn:
        for table in [t.name for t in base.metadata.sorted_tables]:
            if table not in tables_with_id_pk:
                logger.info(
                    "position-stocks oracle autoincrement: skipping %s — PK is not `id`",
                    table,
                )
                continue
            try:
                has_identity = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM user_tab_identity_cols "
                        "WHERE table_name = UPPER(:t) AND column_name = 'ID'"
                    ),
                    {"t": table},
                ).scalar()
            except Exception as e:
                logger.warning("position-stocks oracle identity check failed for %s: %s", table, e)
                continue
            if has_identity:
                continue  # IDENTITY column already present — nothing to do

            seq_name = f"{table}_id_seq"
            trg_name = f"trg_{table}_bi"
            try:
                start_at = conn.execute(
                    text(f"SELECT NVL(MAX(id), 0) + 1 FROM {table}")  # noqa: S608
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
                    # exec_driver_sql (NOT text()) is required: :NEW.id is
                    # Oracle trigger correlation syntax — text() would misparse
                    # it as a SQLAlchemy bind parameter and raise "a value is
                    # required for bind parameter 'NEW'", silently preventing
                    # the trigger from ever being created.
                    trg_conn.exec_driver_sql(
                        f"CREATE OR REPLACE TRIGGER {trg_name} "
                        f"BEFORE INSERT ON {table} FOR EACH ROW "
                        f"WHEN (NEW.id IS NULL) "
                        f"BEGIN SELECT {seq_name}.NEXTVAL INTO :NEW.id FROM dual; END;"
                    )
                logger.info(
                    "position-stocks-db: attached %s / %s to %s (Oracle autoincrement backfill)",
                    seq_name, trg_name, table,
                )
            except Exception as e:
                logger.warning(
                    "position-stocks-db: could not attach autoincrement trigger to %s: %s",
                    table, e,
                )
