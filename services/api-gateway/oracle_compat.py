"""
oracle_compat.py — Neon/Postgres ↔ Oracle Autonomous DB portability shim.

This is the SINGLE place that decides which backend a given DATABASE_URL points
at and builds a python-oracledb (thin mode) SQLAlchemy engine against an Oracle
Autonomous Database using an mTLS Instance Wallet. It also emits the correct
dialect flavour of the small amount of hand-written SQL the KV cache uses
(CREATE TABLE / upsert), so the *same* application code runs unchanged on:

    * Render          -> Neon / Postgres   (DATABASE_URL = postgresql://...)
    * Oracle Cloud VM -> Oracle ADB         (ORACLE_DSN set, or
                                             DATABASE_URL = oracle+oracledb://...)

Env contract — MUST stay in sync with
services/decision-prediction-service/training/models.py:

    ORACLE_DSN              TNS alias, e.g. stockkydb_high  (presence => Oracle mode)
    ORACLE_USER             default ADMIN
    ORACLE_PASSWORD         (or ORACLE_ADMIN_PASSWORD)
    ORACLE_WALLET_DIR       wallet dir (or TNS_ADMIN) — tnsnames.ora/sqlnet.ora/ewallet
    ORACLE_WALLET_PASSWORD  wallet password (mTLS Instance Wallet)
    DB_POOL_SIZE / DB_MAX_OVERFLOW / DB_POOL_RECYCLE / DB_POOL_TIMEOUT

Nothing here imports oracledb unless an Oracle engine is actually built, so
services that only ever talk to Neon pay no import cost.
"""
from __future__ import annotations

import logging
import os

_log = logging.getLogger("oracle-compat")

_ORACLE_LOB_CONFIGURED = False


def oracle_is_configured(url: str = "") -> bool:
    """True when we should talk to Oracle Autonomous DB instead of Postgres.

    Enabled if the URL scheme is oracle, or ORACLE_DSN is set in the env. This is
    exactly what lets the SAME code run on Render (Neon/Postgres) and on the
    Oracle VM (Oracle ADB) with only environment differences."""
    try:
        return (url or "").lower().startswith("oracle") or bool(os.environ.get("ORACLE_DSN"))
    except Exception:
        return False


def dialect_name(engine) -> str:
    """Lower-case SQLAlchemy dialect name ('oracle' | 'postgresql' | 'sqlite')."""
    try:
        return (engine.dialect.name or "").lower()
    except Exception:
        return ""


def is_oracle_engine(engine) -> bool:
    return dialect_name(engine) == "oracle"


def _configure_oracle_lobs() -> None:
    """Return CLOB columns as plain str (not LOB handles) so json.loads() works.

    Safe/no-op on older oracledb builds that lack .defaults.fetch_lobs."""
    global _ORACLE_LOB_CONFIGURED
    if _ORACLE_LOB_CONFIGURED:
        return
    try:
        import oracledb  # noqa: WPS433 (imported lazily, Oracle path only)

        try:
            oracledb.defaults.fetch_lobs = False
        except Exception:
            pass
        _ORACLE_LOB_CONFIGURED = True
    except Exception:
        # oracledb not installed — caller will get a clear error when it builds
        # the engine; nothing to configure here.
        pass


def oracle_engine_kwargs(full_url_provided: bool, **pool_overrides) -> dict:
    """create_engine kwargs for Oracle Autonomous DB via python-oracledb thin mode
    + an mTLS Instance Wallet. Credentials/DSN come from discrete ORACLE_* env
    vars unless a full oracle+oracledb:// URL was supplied.

    pool_overrides lets a caller (e.g. the KV cache on a tiny box) request a
    smaller pool than the training service default."""
    ca: dict = {}
    if not full_url_provided:
        # Empty URL ("oracle+oracledb://") + connect_args — cleanest for wallet
        # auth and avoids URL-encoding the ADMIN password.
        ca["user"] = os.environ.get("ORACLE_USER", "ADMIN")
        pw = os.environ.get("ORACLE_PASSWORD") or os.environ.get("ORACLE_ADMIN_PASSWORD")
        if pw:
            ca["password"] = pw
        ca["dsn"] = os.environ.get("ORACLE_DSN", "")  # TNS alias e.g. stockkydb_high
    # Wallet location applies to both URL and discrete-var forms.
    wallet_dir = os.environ.get("ORACLE_WALLET_DIR") or os.environ.get("TNS_ADMIN")
    wallet_pw = os.environ.get("ORACLE_WALLET_PASSWORD")
    if wallet_dir:
        ca["config_dir"] = wallet_dir
        ca["wallet_location"] = wallet_dir
    if wallet_pw:
        ca["wallet_password"] = wallet_pw

    def _int(name, default):
        return int(pool_overrides.get(name, os.environ.get(name.upper(), default)))

    return {
        "echo": False,
        "pool_pre_ping": True,
        "pool_recycle": _int("db_pool_recycle", "300"),
        "pool_size": _int("db_pool_size", "3"),
        "max_overflow": _int("db_max_overflow", "2"),
        "pool_timeout": _int("db_pool_timeout", "30"),
        "connect_args": ca,
    }


def build_oracle_engine(url: str = "", **pool_overrides):
    """Return (engine, normalized_url) for Oracle Autonomous DB.

    A full 'oracle+oracledb://...' URL is used as-is; otherwise an empty
    'oracle+oracledb://' URL is used and the wallet/DSN/creds ride in
    connect_args (discrete ORACLE_* vars)."""
    from sqlalchemy import create_engine

    _configure_oracle_lobs()
    # "full" = a real oracle URL with an authority/DSN after the scheme. The
    # scheme-only sentinel "oracle+oracledb://" (discrete ORACLE_* vars, no URL)
    # is NOT full, so credentials/DSN ride in connect_args instead.
    body = url.split("://", 1)[1] if "://" in (url or "") else ""
    full = (url or "").lower().startswith("oracle") and bool(body.strip())
    kwargs = oracle_engine_kwargs(full, **pool_overrides)
    if not full:
        url = "oracle+oracledb://"
    _log.info(
        "Oracle Autonomous DB engine (dsn=%s, wallet=%s)",
        os.environ.get("ORACLE_DSN", "from-url"),
        os.environ.get("ORACLE_WALLET_DIR") or os.environ.get("TNS_ADMIN") or "none",
    )
    return create_engine(url, **kwargs), url


# ─────────────────────────────────────────────────────────────────────────────
# Portable SQL for the hand-written KV / settings tables.
# Postgres keeps its original syntax (TEXT / TIMESTAMPTZ / NOW() / ON CONFLICT).
# Oracle gets VARCHAR2/CLOB/TIMESTAMP/SYSTIMESTAMP/MERGE. sqlite is never used by
# the durable layer (no DB URL => memory-only), so only these two branches exist.
# ─────────────────────────────────────────────────────────────────────────────

def now_func(dialect: str) -> str:
    return "SYSTIMESTAMP" if dialect == "oracle" else "NOW()"


def create_table_sql(dialect: str, table: str, with_expires: bool) -> str:
    """DDL for a KV-shaped table. Oracle omits IF NOT EXISTS (caller swallows
    ORA-00955); Postgres keeps it. Oracle CLOB is left NULLable on purpose —
    Oracle coerces '' to NULL, and a NOT NULL CLOB would then reject empty
    values; our JSON payloads are never empty so this is invisible in practice."""
    if dialect == "oracle":
        cols = "k VARCHAR2(1000) PRIMARY KEY, v CLOB"
        if with_expires:
            cols += ", expires_at TIMESTAMP"
        cols += ", updated_at TIMESTAMP DEFAULT SYSTIMESTAMP"
        return f"CREATE TABLE {table} ({cols})"
    cols = "k TEXT PRIMARY KEY, v TEXT NOT NULL"
    if with_expires:
        cols += ", expires_at TIMESTAMPTZ NULL"
    cols += ", updated_at TIMESTAMPTZ DEFAULT NOW()"
    return f"CREATE TABLE IF NOT EXISTS {table} ({cols})"


def create_index_sql(dialect: str, index: str, table: str, col: str) -> str:
    if dialect == "oracle":
        # No IF NOT EXISTS before 23c; caller swallows ORA-00955/ORA-01408.
        return f"CREATE INDEX {index} ON {table} ({col})"
    return f"CREATE INDEX IF NOT EXISTS {index} ON {table} ({col})"


def upsert_sql(dialect: str, table: str, with_expires: bool) -> str:
    """Single-row upsert keyed on k. Binds: :k, :v, (:e when with_expires).

    Postgres -> INSERT ... ON CONFLICT (k) DO UPDATE.
    Oracle   -> MERGE ... USING (SELECT ... FROM dual) ... WHEN MATCHED/NOT."""
    if dialect == "oracle":
        if with_expires:
            using = "SELECT :k AS k, :v AS v, :e AS e FROM dual"
            matched = "UPDATE SET d.v = s.v, d.expires_at = s.e, d.updated_at = SYSTIMESTAMP"
            insert = ("INSERT (k, v, expires_at, updated_at) "
                      "VALUES (s.k, s.v, s.e, SYSTIMESTAMP)")
        else:
            using = "SELECT :k AS k, :v AS v FROM dual"
            matched = "UPDATE SET d.v = s.v, d.updated_at = SYSTIMESTAMP"
            insert = "INSERT (k, v, updated_at) VALUES (s.k, s.v, SYSTIMESTAMP)"
        return (
            f"MERGE INTO {table} d USING ({using}) s ON (d.k = s.k) "
            f"WHEN MATCHED THEN {matched} "
            f"WHEN NOT MATCHED THEN {insert}"
        )
    if with_expires:
        return (
            f"INSERT INTO {table} (k, v, expires_at, updated_at) "
            f"VALUES (:k, :v, :e, NOW()) "
            f"ON CONFLICT (k) DO UPDATE "
            f"SET v = EXCLUDED.v, expires_at = EXCLUDED.expires_at, updated_at = NOW()"
        )
    return (
        f"INSERT INTO {table} (k, v, updated_at) "
        f"VALUES (:k, :v, NOW()) "
        f"ON CONFLICT (k) DO UPDATE "
        f"SET v = EXCLUDED.v, updated_at = NOW()"
    )


def exec_ddl_safe(engine, sql: str, dialect: str) -> None:
    """Run one DDL statement in its own transaction, swallowing 'already exists'.

    Each DDL gets its own begin() so a benign ORA-00955 on one statement cannot
    poison the others. Oracle auto-commits DDL anyway.

    Swallowed Oracle codes:
      ORA-00955  name is already used by an existing object (table/index)
      ORA-00957  duplicate column name
      ORA-01408  such column list already indexed
      ORA-02260  table can have only one primary key
      ORA-02264  name already used by an existing constraint
    The last two matter for tables that declare a NAMED table-level constraint
    (e.g. hotpicks_static_feed's composite PRIMARY KEY), where a re-run can trip
    on the constraint name rather than the table name.
    """
    from sqlalchemy import text

    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
    except Exception as e:  # noqa: BLE001
        m = str(e)
        if dialect == "oracle" and any(
            code in m
            for code in ("ORA-00955", "ORA-01408", "ORA-00957", "ORA-02260", "ORA-02264")
        ):
            return
        if "already exists" in m.lower():
            return
        _log.debug("exec_ddl_safe skip (%s): %s", dialect, m[:160])
