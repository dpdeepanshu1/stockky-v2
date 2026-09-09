"""
Hot Picks static feed schema — Neon/Postgres (Render) AND Oracle Autonomous DB.

Single source of truth for table name + columns + portable SQL, used by:
  - hotpicks_store.py  (writer / reader / freshness / audit)
  - main.py            (/stockky-hot/run, /stockky-hot/table, /stockky-hot/audit)

Table: hotpicks_static_feed
Grain: one row per (symbol, section) — a symbol can legitimately appear in
news_driven AND results_driven AND bulk_insider_driven, so the primary key is
composite. That is what lets the 24h table survive a partial (stopped) scan and
still be merged section-by-section on the next run.

Columns:
  symbol, section, decision, score, news_score, headline_count, signal_strength,
  from_scan, next_earnings_date, summary, item_json, generated_at, updated_at

Backend selection (identical convention to surprise_schema.py / ipo_schema.py):
  ORACLE_DSN set              -> Oracle Autonomous DB (wallet/DSN via ORACLE_*)
  otherwise                   -> CACHE_DATABASE_URL | DATABASE_URL | TRAINING_DATABASE_URL

Deliberate design choices, learned from ipo_static_feed:

  * NO DATE/TIMESTAMP BIND ANYWHERE. next_earnings_date and generated_at are
    stored as ISO *strings* (VARCHAR/VARCHAR2) and updated_at is set by SQL
    (NOW() / SYSTIMESTAMP), never bound from Python. Oracle's TO_DATE needs an
    explicit format and raises ORA-01830/ORA-01858 the moment upstream hands us
    "2026-08-23T10:30:00" or "" instead of "2026-08-23" — Postgres's ::date
    shrugs that off, so a bound date column is exactly where the two backends
    diverge. Removing the bind removes the whole class of bug; freshness is
    computed from the SQL-set updated_at instead.
  * Long text (summary, item_json) is TEXT on Postgres and CLOB on Oracle.
    VARCHAR2 is capped at 4000 bytes, and a Hot Picks item carrying 5 headlines
    blows past that. python-oracledb binds a >4000-byte str as DB_TYPE_LONG
    automatically, so a plain bind into CLOB is safe, and oracle_compat's
    fetch_lobs=False makes reads come back as plain str.
  * Oracle has no BOOLEAN: from_scan is NUMBER(1) and adapt_rows() converts.
  * VARCHAR2 lengths are BYTE budgets on Oracle vs CHARACTER counts on
    Postgres, so adapt_rows() clips by UTF-8 byte length rather than risking
    ORA-12899 on one stray non-ASCII character.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Optional

try:
    import oracle_compat as _oc
except Exception:  # pragma: no cover
    _oc = None

logger = logging.getLogger("hotpicks-schema")

TABLE_NAME = "hotpicks_static_feed"

VALID_SECTIONS = ("news_driven", "results_driven", "bulk_insider_driven")

# ── Postgres DDL ────────────────────────────────────────────────────────────
DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS hotpicks_static_feed (
        symbol VARCHAR(30) NOT NULL,
        section VARCHAR(24) NOT NULL,
        decision VARCHAR(30),
        score NUMERIC(8, 2),
        news_score NUMERIC(8, 2),
        headline_count INTEGER DEFAULT 0,
        signal_strength VARCHAR(10),
        from_scan BOOLEAN DEFAULT FALSE,
        next_earnings_date VARCHAR(20),
        summary TEXT,
        item_json TEXT,
        generated_at VARCHAR(40),
        updated_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (symbol, section)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hotpicks_updated ON hotpicks_static_feed(updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_hotpicks_section ON hotpicks_static_feed(section)",
]

# ── Oracle DDL ─────────────────────────────────────────────────────────────
# Differences forced by Oracle: VARCHAR2/NUMBER/CLOB/TIMESTAMP/SYSTIMESTAMP,
# DEFAULT before NOT NULL, NUMBER(1) instead of BOOLEAN, a table-level PRIMARY
# KEY (an inline column constraint cannot span two columns), and no
# "IF NOT EXISTS" before 23c — exec_ddl_safe() swallows ORA-00955 (table
# exists), ORA-00957 (duplicate column), ORA-01408 (column already indexed) and
# ORA-02264/ORA-02260 (constraint/PK name already in use).
DDL_STATEMENTS_ORACLE = [
    """
    CREATE TABLE hotpicks_static_feed (
        symbol VARCHAR2(30) NOT NULL,
        section VARCHAR2(24) NOT NULL,
        decision VARCHAR2(30),
        score NUMBER(8, 2),
        news_score NUMBER(8, 2),
        headline_count NUMBER(10) DEFAULT 0,
        signal_strength VARCHAR2(10),
        from_scan NUMBER(1) DEFAULT 0,
        next_earnings_date VARCHAR2(20),
        summary CLOB,
        item_json CLOB,
        generated_at VARCHAR2(40),
        updated_at TIMESTAMP DEFAULT SYSTIMESTAMP,
        CONSTRAINT pk_hotpicks_static PRIMARY KEY (symbol, section)
    )
    """,
    "CREATE INDEX idx_hotpicks_updated ON hotpicks_static_feed(updated_at)",
    "CREATE INDEX idx_hotpicks_section ON hotpicks_static_feed(section)",
]

# Explicit column list — never SELECT *
SELECT_COLUMNS = (
    "symbol, section, decision, score, news_score, headline_count, "
    "signal_strength, from_scan, next_earnings_date, summary, item_json, "
    "generated_at, updated_at"
)

# Bind keys the writer supplies per row (updated_at is SQL-set, never bound)
ROW_KEYS = (
    "symbol", "section", "decision", "score", "news_score", "headline_count",
    "signal_strength", "from_scan", "next_earnings_date", "summary",
    "item_json", "generated_at",
)

# Declared VARCHAR2 byte budgets — Oracle counts BYTES, Postgres counts chars.
_ORACLE_VARCHAR2_BYTES = {
    "symbol": 30,
    "section": 24,
    "decision": 30,
    "signal_strength": 10,
    "next_earnings_date": 20,
    "generated_at": 40,
}

# Defensive cap on the CLOB/TEXT payload. The COLUMN is TEXT (Postgres) / CLOB
# (Oracle) so it has no practical size limit on either backend — but the Oracle
# BIND does: python-oracledb sends a str as VARCHAR2 up to Oracle's extended
# datatype limit of 32767 bytes and escalates to DB_TYPE_LONG beyond it, and a
# LONG bind may only be a direct INSERT/UPDATE value for a LONG/CLOB column, not
# a projected expression in a subquery select list — which is precisely where
# MERGE ... USING (SELECT :item_json ... FROM dual) puts it (ORA-01461). Staying
# under the ceiling keeps the bind a plain VARCHAR2 on Oracle and an ordinary
# TEXT insert on Postgres, so one code path is correct on both. The TO_CLOB()
# wrapper in upsert_sql() then makes the select-list datatype explicit rather
# than leaving it to implicit conversion.
JSON_MAX_BYTES = int(os.getenv("HOTPICKS_JSON_MAX_BYTES", "30000"))
SUMMARY_MAX_BYTES = int(os.getenv("HOTPICKS_SUMMARY_MAX_BYTES", "4000"))


def _clip_utf8(value, max_bytes: int):
    """Trim a str so its UTF-8 encoding fits max_bytes, never splitting a char."""
    if not isinstance(value, str):
        return value
    b = value.encode("utf-8")
    if len(b) <= max_bytes:
        return value
    return b[:max_bytes].decode("utf-8", "ignore")


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


def _raw_url() -> str:
    return (
        os.getenv("CACHE_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("TRAINING_DATABASE_URL")
        or ""
    )


def is_oracle() -> bool:
    """True when this process should store the hot-picks feed in Oracle ADB."""
    if _oc is not None:
        try:
            return _oc.oracle_is_configured(_raw_url() or "")
        except Exception:
            pass
    return bool(os.environ.get("ORACLE_DSN"))


def dialect() -> str:
    """'oracle' | 'postgresql' — which SQL flavour to emit."""
    return "oracle" if is_oracle() else "postgresql"


def database_url() -> Optional[str]:
    """Resolved DB URL, or None when no backend is configured at all.

    Oracle: the scheme-only sentinel 'oracle+oracledb://' (credentials/DSN/wallet
    ride in via connect_args in make_engine) — same contract as kv_cache.py,
    surprise_schema.py and ipo_schema.py.
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


def make_engine(app_name: str = "stockky-hotpicks"):
    """Build a SQLAlchemy engine for whichever backend is configured.

    Callers must use this rather than create_engine(database_url()) — the
    Postgres connect_args (connect_timeout, application_name) are psycopg2-only
    and would blow up the Oracle driver.
    """
    url = database_url()
    if not url:
        return None
    from sqlalchemy import create_engine

    if is_oracle():
        if _oc is None:
            logger.warning("hotpicks: ORACLE_DSN set but oracle_compat.py missing")
            return None
        eng, _ = _oc.build_oracle_engine(
            url,
            db_pool_size=os.getenv("HOTPICKS_DB_POOL_SIZE", "2"),
            db_max_overflow=os.getenv("HOTPICKS_DB_MAX_OVERFLOW", "1"),
            db_pool_recycle=os.getenv("HOTPICKS_DB_POOL_RECYCLE", "300"),
            db_pool_timeout=os.getenv("HOTPICKS_DB_POOL_TIMEOUT", "15"),
        )
        return eng

    # ── Neon / Postgres — same kwargs as the other feed modules ──
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


def shared_engine(app_name: str = "stockky-hotpicks"):
    """Process-wide cached engine — build the pool ONCE, not per request.

    make_engine() opens a brand-new pool every call, which means a full TCP +
    TLS handshake (and on the Oracle VM a wallet/mTLS negotiation, which is
    markedly slower) before the first query can even run. For a read path that a
    tab hits on every open — the feed-health panels — that handshake was most of
    the perceived load time. Caching by (app_name, resolved URL) keeps one warm
    pool per process and re-builds automatically if the URL ever changes, so the
    Render/Neon and Oracle paths each get their own correctly-configured engine.

    Callers must NOT dispose() an engine obtained from here: it is shared, and
    disposing it throws away the warm pool for everyone.
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
    """Upsert one row keyed on (symbol, section). Binds: the twelve ROW_KEYS.

    Postgres -> INSERT ... ON CONFLICT (symbol, section) DO UPDATE
    Oracle   -> MERGE ... USING (SELECT ... FROM dual)

    Note for the Oracle branch: symbol/section are the join keys, so they must
    NOT appear in the WHEN MATCHED update list (ORA-38104 — "columns referenced
    in the ON clause cannot be updated").
    """
    dial = dial or dialect()
    if dial == "oracle":
        return """
            MERGE INTO hotpicks_static_feed d
            USING (
                SELECT :symbol AS symbol, :section AS section,
                       :decision AS decision, :score AS score,
                       :news_score AS news_score,
                       :headline_count AS headline_count,
                       :signal_strength AS signal_strength,
                       :from_scan AS from_scan,
                       :next_earnings_date AS next_earnings_date,
                       TO_CLOB(:summary) AS summary,
                       TO_CLOB(:item_json) AS item_json,
                       :generated_at AS generated_at
                FROM dual
            ) s ON (d.symbol = s.symbol AND d.section = s.section)
            WHEN MATCHED THEN UPDATE SET
                d.decision = s.decision,
                d.score = s.score,
                d.news_score = s.news_score,
                d.headline_count = s.headline_count,
                d.signal_strength = s.signal_strength,
                d.from_scan = s.from_scan,
                d.next_earnings_date = s.next_earnings_date,
                d.summary = s.summary,
                d.item_json = s.item_json,
                d.generated_at = s.generated_at,
                d.updated_at = SYSTIMESTAMP
            WHEN NOT MATCHED THEN INSERT
                (symbol, section, decision, score, news_score, headline_count,
                 signal_strength, from_scan, next_earnings_date, summary,
                 item_json, generated_at, updated_at)
            VALUES
                (s.symbol, s.section, s.decision, s.score, s.news_score,
                 s.headline_count, s.signal_strength, s.from_scan,
                 s.next_earnings_date, s.summary, s.item_json, s.generated_at,
                 SYSTIMESTAMP)
        """
    return """
        INSERT INTO hotpicks_static_feed
            (symbol, section, decision, score, news_score, headline_count,
             signal_strength, from_scan, next_earnings_date, summary,
             item_json, generated_at, updated_at)
        VALUES
            (:symbol, :section, :decision, :score, :news_score, :headline_count,
             :signal_strength, :from_scan, :next_earnings_date, :summary,
             :item_json, :generated_at, NOW())
        ON CONFLICT (symbol, section) DO UPDATE SET
            decision = EXCLUDED.decision,
            score = EXCLUDED.score,
            news_score = EXCLUDED.news_score,
            headline_count = EXCLUDED.headline_count,
            signal_strength = EXCLUDED.signal_strength,
            from_scan = EXCLUDED.from_scan,
            next_earnings_date = EXCLUDED.next_earnings_date,
            summary = EXCLUDED.summary,
            item_json = EXCLUDED.item_json,
            generated_at = EXCLUDED.generated_at,
            updated_at = NOW()
    """


def select_recent_sql(dial: Optional[str] = None) -> str:
    """Rows written within the last :hours hours. Bind :hours (int).

    Both branches compare against the SQL-set updated_at, so no Python datetime
    ever crosses the driver boundary — the one place these two dialects are
    hardest to keep in agreement.
    """
    dial = dial or dialect()
    if dial == "oracle":
        return (
            f"SELECT {SELECT_COLUMNS} FROM hotpicks_static_feed "
            "WHERE updated_at >= SYSTIMESTAMP - NUMTODSINTERVAL(:hours, 'HOUR') "
            "ORDER BY section, score DESC NULLS LAST"
        )
    return (
        f"SELECT {SELECT_COLUMNS} FROM hotpicks_static_feed "
        "WHERE updated_at >= NOW() - (:hours * INTERVAL '1 hour') "
        "ORDER BY section, score DESC NULLS LAST"
    )


def delete_older_than_sql(dial: Optional[str] = None) -> str:
    """Prune rows older than :hours hours. Bind :hours (int)."""
    dial = dial or dialect()
    if dial == "oracle":
        return (
            "DELETE FROM hotpicks_static_feed "
            "WHERE updated_at < SYSTIMESTAMP - NUMTODSINTERVAL(:hours, 'HOUR')"
        )
    return (
        "DELETE FROM hotpicks_static_feed "
        "WHERE updated_at < NOW() - (:hours * INTERVAL '1 hour')"
    )


def adapt_rows(rows: list, dial: Optional[str] = None) -> list:
    """Make bind values safe for the target driver.

    Oracle: from_scan bool -> 1/0 (NUMBER(1)); VARCHAR2 values clipped to their
    declared BYTE budget so one non-ASCII char cannot raise ORA-12899 and drop
    the row. Postgres rows pass through with only the bool defaulted, keeping
    the Render path behaviourally identical to a plain INSERT.
    """
    dial = dial or dialect()
    out = []
    for r in rows:
        r2 = dict(r)
        # Applies to BOTH backends: keep the CLOB/TEXT payload bounded.
        if isinstance(r2.get("item_json"), str):
            r2["item_json"] = _clip_utf8(r2["item_json"], JSON_MAX_BYTES)
        if isinstance(r2.get("summary"), str):
            r2["summary"] = _clip_utf8(r2["summary"], SUMMARY_MAX_BYTES)
        if dial == "oracle":
            fs = r2.get("from_scan")
            r2["from_scan"] = 1 if fs else 0
            for col, limit in _ORACLE_VARCHAR2_BYTES.items():
                v = r2.get(col)
                if isinstance(v, str):
                    r2[col] = _clip_utf8(v, limit)
        else:
            r2["from_scan"] = bool(r2.get("from_scan"))
        out.append(r2)
    return out


def coerce_bool(value) -> bool:
    """Read-side inverse of adapt_rows: Oracle hands back 1/0, Postgres True/False."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return str(value).strip().lower() in ("true", "t", "y", "yes", "1")


def ensure_hotpicks_schema() -> dict:
    """Create table/indexes if missing. Safe to call before every hot-picks run."""
    url = database_url()
    if not url:
        return {"ok": False, "error": "No DATABASE_URL / CACHE_DATABASE_URL configured"}
    eng = None
    try:
        from sqlalchemy import text

        dial = dialect()
        eng = make_engine("stockky-hotpicks-schema")
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

        logger.info("hotpicks_static_feed schema ready (backend=%s)", dial)
        return {"ok": True, "table": TABLE_NAME, "backend": dial}
    except Exception as e:
        logger.warning("ensure_hotpicks_schema failed: %s", e)
        return {"ok": False, "error": str(e)[:240]}
    finally:
        if eng is not None:
            try:
                eng.dispose()
            except Exception:
                pass
