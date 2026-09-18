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
    _ensure_product_type_columns(eng, dialect())
    _ensure_overnight_hold_columns(eng, dialect())
    _backfill_broker_imported_flag(eng)
    _ensure_watchlist_link_columns(eng, dialect())
    _fix_stale_dhan_token_expiry(eng)
    _ensure_account_columns(eng, dialect())
    _ensure_candidate_overnight_column(eng, dialect())
    _ensure_source_tab_columns(eng, dialect())
    _ensure_catalyst_price_source_column(eng, dialect())
    _ensure_hot_path_indexes(eng, dialect())
    _ensure_exit_retry_columns(eng, dialect())
    _ensure_afterhours_gate_column(eng, dialect())
    _ensure_nextday_watchlist_indexes(eng, dialect())
    _ensure_afterhours_last_run_columns(eng, dialect())


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
# trade_accounts existed before realized_pnl_total was added to models.py
# (2026-09-09, needed by the dashboard's PortfolioSummary all-time P&L
# readout — see main.py's /status/{mode} and portfolio.py's
# record_real_exit_fill / close_position). On any already-deployed DB this
# column must be added once; on first boot create_all() creates it directly.
def _ensure_account_columns(engine, dialect_name: str) -> None:
    from sqlalchemy import inspect, text

    try:
        existing = {c["name"] for c in inspect(engine).get_columns("trade_accounts")}
    except Exception as e:
        logger.warning("real-trade-db: could not inspect trade_accounts columns: %s", e)
        return

    if dialect_name == "oracle":
        adds = [
            ("realized_pnl_total", "ALTER TABLE trade_accounts ADD (realized_pnl_total FLOAT DEFAULT 0.0 NOT NULL)"),
            # ADDED (session52, capital-split fix) — see models.py's comment
            # on this column for the full reasoning.
            ("broker_cash_available", "ALTER TABLE trade_accounts ADD (broker_cash_available FLOAT DEFAULT 0.0 NOT NULL)"),
            # ADDED (this session, realized_pnl_today daily-reset fix) — see
            # models.py's comment on this column for the full reasoning.
            ("pnl_last_reset_date", "ALTER TABLE trade_accounts ADD (pnl_last_reset_date VARCHAR2(10))"),
        ]
    else:
        adds = [
            ("realized_pnl_total", "ALTER TABLE trade_accounts ADD COLUMN realized_pnl_total FLOAT DEFAULT 0.0 NOT NULL"),
            ("broker_cash_available", "ALTER TABLE trade_accounts ADD COLUMN broker_cash_available FLOAT DEFAULT 0.0 NOT NULL"),
            ("pnl_last_reset_date", "ALTER TABLE trade_accounts ADD COLUMN pnl_last_reset_date VARCHAR(10)"),
        ]

    for col_name, sql in adds:
        if col_name in existing:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
            logger.info("real-trade-db: added trade_accounts.%s", col_name)
        except Exception as e:
            m = str(e)
            if "already exists" in m.lower() or "ORA-01430" in m:
                continue
            logger.warning("real-trade-db: could not add trade_accounts.%s: %s", col_name, e)


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
            # 2026-09-10 (session22): fourth scheduled feature — see models.py.
            ("eod_signal_scan_enabled", "ALTER TABLE trade_gate_state ADD (eod_signal_scan_enabled NUMBER(1) DEFAULT 0 NOT NULL)"),
            ("eod_signal_scan_enabled_at", "ALTER TABLE trade_gate_state ADD (eod_signal_scan_enabled_at TIMESTAMP)"),
            ("eod_signal_scan_last_run", "ALTER TABLE trade_gate_state ADD (eod_signal_scan_last_run VARCHAR2(10))"),
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
            # 2026-09-10 (session22): fourth scheduled feature — see models.py.
            ("eod_signal_scan_enabled", "ALTER TABLE trade_gate_state ADD COLUMN eod_signal_scan_enabled BOOLEAN DEFAULT FALSE NOT NULL"),
            ("eod_signal_scan_enabled_at", "ALTER TABLE trade_gate_state ADD COLUMN eod_signal_scan_enabled_at TIMESTAMP"),
            ("eod_signal_scan_last_run", "ALTER TABLE trade_gate_state ADD COLUMN eod_signal_scan_last_run VARCHAR(10)"),
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


# Same additive-migration idiom as _ensure_manual_order_columns above —
# trade_candidates existed before overnight_priority was added to models.py
# (2026-09-10, session22 — EOD signal scan / overnight-priority feature).
def _ensure_candidate_overnight_column(engine, dialect_name: str) -> None:
    from sqlalchemy import inspect, text

    try:
        existing = {c["name"] for c in inspect(engine).get_columns("trade_candidates")}
    except Exception as e:
        logger.warning("real-trade-db: could not inspect trade_candidates columns: %s", e)
        return

    if dialect_name == "oracle":
        adds = [
            ("overnight_priority", "ALTER TABLE trade_candidates ADD (overnight_priority NUMBER(1) DEFAULT 0 NOT NULL)"),
            # 2026-09-11 (session23): see models.py TradeCandidate.us_sector_bonus.
            ("us_sector_bonus", "ALTER TABLE trade_candidates ADD (us_sector_bonus BINARY_DOUBLE DEFAULT 0 NOT NULL)"),
        ]
    else:
        adds = [
            ("overnight_priority", "ALTER TABLE trade_candidates ADD COLUMN overnight_priority BOOLEAN DEFAULT FALSE NOT NULL"),
            ("us_sector_bonus", "ALTER TABLE trade_candidates ADD COLUMN us_sector_bonus DOUBLE PRECISION DEFAULT 0 NOT NULL"),
        ]

    for col_name, sql in adds:
        if col_name in existing:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
            logger.info("real-trade-db: added trade_candidates.%s", col_name)
        except Exception as e:
            m = str(e)
            if "already exists" in m.lower() or "ORA-01430" in m:
                continue
            logger.warning("real-trade-db: could not add trade_candidates.%s: %s", col_name, e)


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
            # 2026-09-09 fix — see models.py TradePosition.broker_imported
            # docstring: distinguishes a pre-existing demat holding
            # (import_broker_holdings) from a position this system actually
            # bought, so exit.py can force CNC for it regardless of the
            # import-time opened_at. Existing rows default to 0/False, which
            # is correct for every position opened via entry_engine/
            # manual_engine — only newly-imported holdings ever need True,
            # and import_broker_holdings sets it explicitly going forward.
            ("broker_imported", "ALTER TABLE trade_positions ADD (broker_imported NUMBER(1) DEFAULT 0 NOT NULL)"),
        ]
    else:
        adds = [
            ("initial_stop_distance", "ALTER TABLE trade_positions ADD COLUMN initial_stop_distance FLOAT"),
            ("broker_imported", "ALTER TABLE trade_positions ADD COLUMN broker_imported BOOLEAN DEFAULT FALSE NOT NULL"),
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


# 2026-09-15 fix (session40 — see models.py TradePosition.consecutive_exit_
# failures docstring for the full DATAMATICS incident). Same additive-
# migration idiom as _ensure_position_columns above — trade_positions
# existed before these columns were added to models.py, so on any
# already-deployed DB they must be added by hand, once. Both nullable/
# zero-defaulted so every existing row simply starts at "never failed",
# which is correct — this is a from-now-on counter, not a backfill.
def _ensure_exit_retry_columns(engine, dialect_name: str) -> None:
    from sqlalchemy import inspect, text

    try:
        existing = {c["name"] for c in inspect(engine).get_columns("trade_positions")}
    except Exception as e:
        logger.warning("real-trade-db: could not inspect trade_positions columns (exit-retry): %s", e)
        return

    if dialect_name == "oracle":
        adds = [
            ("consecutive_exit_failures", "ALTER TABLE trade_positions ADD (consecutive_exit_failures NUMBER(5) DEFAULT 0 NOT NULL)"),
            ("last_exit_failure_at", "ALTER TABLE trade_positions ADD (last_exit_failure_at TIMESTAMP)"),
        ]
    else:
        adds = [
            ("consecutive_exit_failures", "ALTER TABLE trade_positions ADD COLUMN consecutive_exit_failures INTEGER DEFAULT 0 NOT NULL"),
            ("last_exit_failure_at", "ALTER TABLE trade_positions ADD COLUMN last_exit_failure_at TIMESTAMP"),
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


# 2026-09-09 data fixup: broker_imported (just added above, defaults False)
# is only ever set True going forward by portfolio.import_broker_holdings —
# it has no way to retroactively know about rows that import already
# created BEFORE this column existed. Those rows are exactly the ones
# stuck in the "insufficient funds" SELL-rejection loop the column exists
# to fix (see models.py TradePosition.broker_imported and
# exit_engine.exit._send_real_sell's docstrings), so leaving them at the
# default would mean the fix only applies to future imports and every
# already-open broker-imported position keeps failing. import_broker_
# holdings has always written a TradePositionEvent(event_type="OPENED",
# detail="Imported from Dhan demat holdings...") in the same transaction
# as creating the position — that's a reliable, already-existing breadcrumb
# to backfill from. One-time and idempotent: only rows still at
# broker_imported=False get touched, so a row already correctly flagged
# (or a genuinely-not-imported position that happens to match, which
# shouldn't occur since only import_broker_holdings ever writes this exact
# detail text) is never re-processed after its first pass.
def _backfill_broker_imported_flag(engine) -> None:
    from sqlalchemy import text

    try:
        with engine.begin() as conn:
            result = conn.execute(text(
                "UPDATE trade_positions SET broker_imported = TRUE "
                "WHERE broker_imported = FALSE AND id IN ("
                "  SELECT position_id FROM trade_position_events "
                "  WHERE event_type = 'OPENED' "
                "  AND detail LIKE 'Imported from Dhan demat holdings%'"
                ")"
            ) if dialect() != "oracle" else text(
                "UPDATE trade_positions SET broker_imported = 1 "
                "WHERE broker_imported = 0 AND id IN ("
                "  SELECT position_id FROM trade_position_events "
                "  WHERE event_type = 'OPENED' "
                "  AND detail LIKE 'Imported from Dhan demat holdings%'"
                ")"
            ))
            if result.rowcount:
                logger.info(
                    "real-trade-db: backfilled broker_imported=True on %s pre-existing "
                    "imported-holding position(s) — see 2026-09-09 insufficient-funds fix",
                    result.rowcount,
                )
    except Exception as e:
        logger.warning("real-trade-db: could not backfill broker_imported flag: %s", e)


# Short-Term Trading Upgrade (2026-09-02): trade_candidates, trade_orders,
# and trade_positions all existed before watchlist_entry_id was added to
# models.py — same additive-migration idiom as every _ensure_* fn above.
# trade_watchlist/trade_resilience_cache are brand-new tables so create_all()
# handles them; only the FK-carrying columns on pre-existing tables need this.
# All three are nullable and default NULL, so every pre-existing row and every
# future non-watchlist row is completely unaffected.
# 2026-09-15 fix (session38 — DATAMATICS "insufficient funds" SELL
# rejections). See models.py TradeOrder.product_type / TradePosition.
# entry_product_type docstrings for the full incident. Both columns are
# nullable and left NULL on existing rows — exit_engine falls back to its
# previous same-day heuristic for any position that already has no
# entry_product_type recorded, so this migration changes nothing for
# positions already open when it ships; it only takes effect for BUYs
# placed after this deploy.
def _ensure_product_type_columns(engine, dialect_name: str) -> None:
    from sqlalchemy import inspect, text

    try:
        order_cols = {c["name"] for c in inspect(engine).get_columns("trade_orders")}
        position_cols = {c["name"] for c in inspect(engine).get_columns("trade_positions")}
    except Exception as e:
        logger.warning("real-trade-db: could not inspect columns for product_type migration: %s", e)
        return

    if dialect_name == "oracle":
        adds = [
            ("trade_orders", order_cols, "product_type",
             "ALTER TABLE trade_orders ADD (product_type VARCHAR2(16))"),
            ("trade_positions", position_cols, "entry_product_type",
             "ALTER TABLE trade_positions ADD (entry_product_type VARCHAR2(16))"),
        ]
    else:
        adds = [
            ("trade_orders", order_cols, "product_type",
             "ALTER TABLE trade_orders ADD COLUMN product_type VARCHAR(16)"),
            ("trade_positions", position_cols, "entry_product_type",
             "ALTER TABLE trade_positions ADD COLUMN entry_product_type VARCHAR(16)"),
        ]

    for table, existing, col_name, sql in adds:
        if col_name in existing:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
            logger.info("real-trade-db: added %s.%s", table, col_name)
        except Exception as e:
            m = str(e)
            if "already exists" in m.lower() or "ORA-01430" in m:
                continue
            logger.warning("real-trade-db: could not add %s.%s: %s", table, col_name, e)


# 2026-09-18 fix (selective overnight hold — see models.py TradeOrder/
# TradePosition entry_decision_label / entry_conviction_score docstrings and
# config.py's OVERNIGHT_HOLD_* block for the full incident). Both columns are
# nullable and left NULL on existing rows — auto_pilot._select_overnight_holds
# simply never grants overnight-hold eligibility to a position with no
# entry_decision_label, so this migration changes nothing for positions
# already open when it ships (they keep squaring off exactly as before); it
# only takes effect for BUYs placed after this deploy.
def _ensure_overnight_hold_columns(engine, dialect_name: str) -> None:
    from sqlalchemy import inspect, text

    try:
        order_cols = {c["name"] for c in inspect(engine).get_columns("trade_orders")}
        position_cols = {c["name"] for c in inspect(engine).get_columns("trade_positions")}
    except Exception as e:
        logger.warning("real-trade-db: could not inspect columns for overnight_hold migration: %s", e)
        return

    if dialect_name == "oracle":
        adds = [
            ("trade_orders", order_cols, "entry_decision_label",
             "ALTER TABLE trade_orders ADD (entry_decision_label VARCHAR2(32))"),
            ("trade_orders", order_cols, "entry_conviction_score",
             "ALTER TABLE trade_orders ADD (entry_conviction_score BINARY_DOUBLE)"),
            ("trade_positions", position_cols, "entry_decision_label",
             "ALTER TABLE trade_positions ADD (entry_decision_label VARCHAR2(32))"),
            ("trade_positions", position_cols, "entry_conviction_score",
             "ALTER TABLE trade_positions ADD (entry_conviction_score BINARY_DOUBLE)"),
        ]
    else:
        adds = [
            ("trade_orders", order_cols, "entry_decision_label",
             "ALTER TABLE trade_orders ADD COLUMN entry_decision_label VARCHAR(32)"),
            ("trade_orders", order_cols, "entry_conviction_score",
             "ALTER TABLE trade_orders ADD COLUMN entry_conviction_score FLOAT"),
            ("trade_positions", position_cols, "entry_decision_label",
             "ALTER TABLE trade_positions ADD COLUMN entry_decision_label VARCHAR(32)"),
            ("trade_positions", position_cols, "entry_conviction_score",
             "ALTER TABLE trade_positions ADD COLUMN entry_conviction_score FLOAT"),
        ]

    for table, existing, col_name, sql in adds:
        if col_name in existing:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
            logger.info("real-trade-db: added %s.%s", table, col_name)
        except Exception as e:
            m = str(e)
            if "already exists" in m.lower() or "ORA-01430" in m:
                continue
            logger.warning("real-trade-db: could not add %s.%s: %s", table, col_name, e)


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


# Same additive-migration idiom as _ensure_watchlist_link_columns above —
# 2026-09-12 fix (audit finding — volume_shock 10-day time-stop bug, see
# models.py TradePosition.source_tab docstring): trade_orders and
# trade_positions both existed before source_tab was added, so an
# already-deployed DB needs this column added by hand, once. Nullable and
# NULL for every pre-existing row — exit_engine._load_profile only special-
# cases source_tab="volume_shock" and falls back to prior behavior for
# NULL/anything else, so this is a pure addition with no read-path change
# for existing data.
def _ensure_source_tab_columns(engine, dialect_name: str) -> None:
    from sqlalchemy import inspect, text

    targets = [
        ("trade_orders",    "source_tab"),
        ("trade_positions", "source_tab"),
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
            sql = f"ALTER TABLE {table_name} ADD ({col_name} VARCHAR2(32))"
        else:
            sql = f"ALTER TABLE {table_name} ADD COLUMN {col_name} VARCHAR(32)"
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
            logger.info("real-trade-db: added %s.%s", table_name, col_name)
        except Exception as e:
            m = str(e)
            if "already exists" in m.lower() or "ORA-01430" in m:
                continue
            logger.warning("real-trade-db: could not add %s.%s: %s", table_name, col_name, e)


# AUDIT FIX (this session): trade_watchlist predates catalyst_price_source
# (see models.py's WatchlistEntry docstring for why this column exists) —
# same additive-migration idiom as every _ensure_* fn above. Nullable, no
# default: every pre-existing row reads as NULL ("unknown, pre-migration"),
# which is exactly correct — we genuinely don't know whether those rows'
# catalyst_price came from a live tick or a stale close, and NULL says so
# rather than guessing.
def _ensure_catalyst_price_source_column(engine, dialect_name: str) -> None:
    from sqlalchemy import inspect, text

    table_name, col_name = "trade_watchlist", "catalyst_price_source"
    try:
        existing = {c["name"] for c in inspect(engine).get_columns(table_name)}
    except Exception as e:
        logger.warning("real-trade-db: could not inspect %s columns: %s", table_name, e)
        return
    if col_name in existing:
        return
    if dialect_name == "oracle":
        sql = f"ALTER TABLE {table_name} ADD ({col_name} VARCHAR2(16))"
    else:
        sql = f"ALTER TABLE {table_name} ADD COLUMN {col_name} VARCHAR(16)"
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
        logger.info("real-trade-db: added %s.%s", table_name, col_name)
    except Exception as e:
        m = str(e)
        if "already exists" in m.lower() or "ORA-01430" in m:
            return
        logger.warning("real-trade-db: could not add %s.%s: %s", table_name, col_name, e)


# SESSION 33 AUDIT FIX (index/performance): create_all(checkfirst=True) only
# ever adds an index to a table it is ALSO creating for the first time — it
# never retrofits an index onto a table that already exists (same limitation
# documented above for columns; SQLAlchemy diffs table names only). Both of
# these tables predate the Index(...) declarations now in models.py, so on
# any already-deployed DB neither index actually exists yet, even though the
# model file claims them.
#
# trade_orders — GET /orders/{mode} (main.py's list_orders) filters by mode
# and a created_at cutoff, then orders by created_at DESC, on every Orders
# tab poll. No supporting index -> full table scan, growing slower every day
# as orders accumulate.
#
# trade_candidates — entry_engine.evaluate_mode's `filter_by(mode=mode,
# consumed=False).order_by(received_at.asc())` is the query auto_pilot's
# full cycle runs every tick, forever (AUTO_PILOT_INTERVAL_SECONDS, see
# execution/auto_pilot.py) — almost certainly the single most-executed query
# in this service. Also had no supporting index.
#
# Uses the same create_index_sql/exec_ddl_safe idiom api-gateway's
# hotpicks_schema.py / surprise_schema.py already use elsewhere in this
# codebase: CREATE INDEX IF NOT EXISTS on Postgres, plain CREATE INDEX on
# Oracle (no IF NOT EXISTS before 23c) with ORA-00955/ORA-01408 swallowed by
# exec_ddl_safe on a re-run. Online on Oracle — no downtime, no table lock
# for the duration real-trade-service would notice.
def _ensure_hot_path_indexes(engine, dialect_name: str) -> None:
    indexes = [
        ("ix_trade_orders_mode_created", "trade_orders", "mode, created_at"),
        ("ix_trade_candidates_mode_consumed_recv", "trade_candidates", "mode, consumed, received_at"),
    ]
    for index_name, table, cols in indexes:
        sql = _oc.create_index_sql(dialect_name, index_name, table, cols)
        _oc.exec_ddl_safe(engine, sql, dialect_name)
        logger.info("real-trade-db: ensured index %s on %s", index_name, table)


def _ensure_oracle_autoincrement(engine, base) -> None:
    from sqlalchemy import text

    # BUG FIX (2026-09-02, ORA-00904 "ID: invalid identifier"): this used to
    # iterate every trade_* table unconditionally and run SELECT/DDL that
    # hard-assumes an `id` column exists. trade_resilience_cache has PK=`key`
    # (String(64)), no `id` column at all — that unconditional SELECT NVL(MAX(id)...)
    # is exactly what threw ORA-00904 on every cold start. Skip any table
    # whose primary key doesn't include `id`; zero schema change otherwise.
    tables_with_id_pk = {
        t.name
        for t in base.metadata.sorted_tables
        if "id" in {c.name for c in t.primary_key.columns}
    }
    tables = [t.name for t in base.metadata.sorted_tables]
    with engine.connect() as conn:
        for table in tables:
            if table not in tables_with_id_pk:
                logger.info(
                    "oracle autoincrement: skipping %s — primary key is not `id`",
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


# ── After-hours gate columns (2026-09-17, session56) ────────────────────────
# trade_gate_state existed before afterhours_news_scan_enabled /
# afterhours_news_scan_enabled_at were added to models.py — on any
# already-deployed DB these columns must be added by the migration below.
# Same additive-migration idiom as every _ensure_* above.
def _ensure_afterhours_gate_column(engine, dialect_name: str) -> None:
    # Matches the _ensure_candidate_overnight_column pattern: Oracle uses
    # ADD (col TYPE DEFAULT val NOT NULL) with parentheses; Postgres uses
    # ADD COLUMN col TYPE DEFAULT val NOT NULL. Each column gets its own
    # engine.begin() so an ORA-01430 (column already exists) on one never
    # poisons the other — same approach as every other _ensure_* function.
    from sqlalchemy import inspect, text

    try:
        existing = {c["name"] for c in inspect(engine).get_columns("trade_gate_state")}
    except Exception as e:
        logger.warning("real-trade-db: could not inspect trade_gate_state columns: %s", e)
        return

    if dialect_name == "oracle":
        adds = [
            (
                "afterhours_news_scan_enabled",
                "ALTER TABLE trade_gate_state ADD (afterhours_news_scan_enabled NUMBER(1) DEFAULT 0 NOT NULL)",
            ),
            (
                "afterhours_news_scan_enabled_at",
                "ALTER TABLE trade_gate_state ADD (afterhours_news_scan_enabled_at TIMESTAMP NULL)",
            ),
            (
                "afterhours_finalize_last_run",
                "ALTER TABLE trade_gate_state ADD (afterhours_finalize_last_run VARCHAR2(10) NULL)",
            ),
        ]
    else:
        adds = [
            (
                "afterhours_news_scan_enabled",
                "ALTER TABLE trade_gate_state ADD COLUMN afterhours_news_scan_enabled BOOLEAN DEFAULT FALSE NOT NULL",
            ),
            (
                "afterhours_news_scan_enabled_at",
                "ALTER TABLE trade_gate_state ADD COLUMN afterhours_news_scan_enabled_at TIMESTAMP NULL",
            ),
            (
                "afterhours_finalize_last_run",
                "ALTER TABLE trade_gate_state ADD COLUMN afterhours_finalize_last_run VARCHAR(10) NULL",
            ),
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


# ── After-hours scan last-run columns (2026-09-17, session58) ──────────────
# trade_gate_state existed before afterhours_scan_last_run_at /
# afterhours_scan_last_run_ok were added to models.py — additive migration,
# same idiom as _ensure_afterhours_gate_column above.
def _ensure_afterhours_last_run_columns(engine, dialect_name: str) -> None:
    from sqlalchemy import inspect, text

    try:
        existing = {c["name"] for c in inspect(engine).get_columns("trade_gate_state")}
    except Exception as e:
        logger.warning("real-trade-db: could not inspect trade_gate_state columns: %s", e)
        return

    if dialect_name == "oracle":
        adds = [
            (
                "afterhours_scan_last_run_at",
                "ALTER TABLE trade_gate_state ADD (afterhours_scan_last_run_at TIMESTAMP NULL)",
            ),
            (
                "afterhours_scan_last_run_ok",
                "ALTER TABLE trade_gate_state ADD (afterhours_scan_last_run_ok NUMBER(1) NULL)",
            ),
        ]
    else:
        adds = [
            (
                "afterhours_scan_last_run_at",
                "ALTER TABLE trade_gate_state ADD COLUMN afterhours_scan_last_run_at TIMESTAMP NULL",
            ),
            (
                "afterhours_scan_last_run_ok",
                "ALTER TABLE trade_gate_state ADD COLUMN afterhours_scan_last_run_ok BOOLEAN NULL",
            ),
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


# ── NextDayWatchlist indexes (2026-09-17, session56) ────────────────────────
# trade_nextday_watchlist is a new table — create_all(checkfirst=True) will
# create it fresh on any DB that doesn't have it yet. On Oracle we also need
# to ensure the explicit indexes defined in models.py are present (they're
# part of Table metadata so create_all covers Postgres automatically, but
# Oracle's implicit index from a UniqueConstraint may differ from a plain
# Index — safer to call exec_ddl_safe explicitly for both dialects).
def _ensure_nextday_watchlist_indexes(engine, dialect_name: str) -> None:
    indexes = [
        ("ix_nextday_watchlist_mode_date_consumed", "trade_nextday_watchlist", "mode, market_date, consumed"),
        ("ix_nextday_watchlist_mode_sym_date", "trade_nextday_watchlist", "mode, symbol, market_date"),
    ]
    import oracle_compat as _oc
    for index_name, table, cols in indexes:
        sql = _oc.create_index_sql(dialect_name, index_name, table, cols)
        _oc.exec_ddl_safe(engine, sql, dialect_name)
        logger.info("real-trade-db: ensured index %s on %s", index_name, table)
