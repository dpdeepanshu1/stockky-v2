"""
IPO static feed schema — Neon/Postgres (Render) AND Oracle Autonomous DB.

Mirrors surprise_schema.py's exact pattern/conventions (same engine builder,
same dialect selection, same Oracle-safe DDL approach) — see that file's
docstring for the reasoning; this one is the IPO Tracker tab's equivalent
of surprise_static_feed.

Why this table exists: previously every "Scan" click on the IPO section
re-fetched NSE's public-past-issues + all-upcoming-issues live, scored
in-memory, and cached the SCORED RESULT for a while in kv_cache — but never
kept the raw discovered rows anywhere durable. That meant:
  - no way to see "what did NSE actually return" separately from "what did
    the scorer do with it" when debugging a parsing bug
  - every scan re-did the recency-window/date-parsing/price-band logic from
    scratch instead of being able to say "we already know about this IPO,
    just refresh its live fields (subscription/GMP)"
  - no natural 24h-freshness story like the Surprise premarket table has

Table: ipo_static_feed
Columns:
  symbol, company_name, issue_price, listing_date, listing_date_estimated,
  issue_start_date, issue_end_date, stage, nse_status, subscription_times,
  gmp, source, ipo_score, decision, buy_suggestion_json, updated_at
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

try:
    import oracle_compat as _oc
except Exception:  # pragma: no cover
    _oc = None

logger = logging.getLogger("ipo-schema")

TABLE_NAME = "ipo_static_feed"

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS ipo_static_feed (
        symbol VARCHAR(30) PRIMARY KEY,
        company_name VARCHAR(160),
        issue_price NUMERIC(12, 2),
        listing_date DATE,
        listing_date_estimated BOOLEAN DEFAULT FALSE,
        issue_start_date VARCHAR(20),
        issue_end_date VARCHAR(20),
        stage VARCHAR(20),
        nse_status VARCHAR(30),
        subscription_times NUMERIC(10, 2),
        gmp NUMERIC(12, 2),
        source VARCHAR(20),
        ipo_score NUMERIC(6, 2),
        decision VARCHAR(30),
        buy_suggestion_json TEXT,
        updated_at TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ipo_static_updated ON ipo_static_feed(updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_ipo_static_stage ON ipo_static_feed(stage)",
]

DDL_STATEMENTS_ORACLE = [
    """
    CREATE TABLE ipo_static_feed (
        symbol VARCHAR2(30) PRIMARY KEY,
        company_name VARCHAR2(160),
        issue_price NUMBER(12, 2),
        listing_date DATE,
        listing_date_estimated NUMBER(1) DEFAULT 0,
        issue_start_date VARCHAR2(20),
        issue_end_date VARCHAR2(20),
        stage VARCHAR2(20),
        nse_status VARCHAR2(30),
        subscription_times NUMBER(10, 2),
        gmp NUMBER(12, 2),
        source VARCHAR2(20),
        ipo_score NUMBER(6, 2),
        decision VARCHAR2(30),
        buy_suggestion_json CLOB,
        updated_at TIMESTAMP DEFAULT SYSTIMESTAMP
    )
    """,
    "CREATE INDEX idx_ipo_static_updated ON ipo_static_feed(updated_at)",
    "CREATE INDEX idx_ipo_static_stage ON ipo_static_feed(stage)",
]

SELECT_COLUMNS = (
    "symbol, company_name, issue_price, listing_date, listing_date_estimated, "
    "issue_start_date, issue_end_date, stage, nse_status, subscription_times, "
    "gmp, source, ipo_score, decision, buy_suggestion_json, updated_at"
)

# Declared VARCHAR2 byte budgets (Oracle counts BYTES by default, Postgres
# counts characters) — used by adapt_rows() to clip instead of raising ORA-12899.
_ORACLE_VARCHAR2_BYTES = {
    "symbol": 30,
    "company_name": 160,
    "issue_start_date": 20,
    "issue_end_date": 20,
    "stage": 20,
    "nse_status": 30,
    "source": 20,
    "decision": 30,
}

# Rows written by an earlier build used this sentinel for "listing date unknown".
# ensure_ipo_schema() heals them to NULL on Oracle so readers never see a fake
# 1900 listing. Kept as a constant so there is exactly one definition of the hack
# being retired.
_LEGACY_NULL_DATE_SENTINEL = "1900-01-01"

# buy_suggestion_json is TEXT on Postgres and CLOB on Oracle, so the COLUMN has
# no practical size limit on either backend. The BIND does, and only on Oracle:
# python-oracledb sends a str as VARCHAR2 up to Oracle's extended-datatype limit
# of 32767 bytes and escalates to DB_TYPE_LONG beyond it. A LONG bind is legal
# only as a direct INSERT/UPDATE value into a LONG/CLOB column — NOT as a
# projected expression in a subquery select list, which is exactly where our
# MERGE ... USING (SELECT :buy_suggestion_json ... FROM dual) puts it. Oversized
# suggestions would therefore raise ORA-01461 on the Oracle VM while writing
# perfectly fine on Render, i.e. a data-dependent, backend-specific failure.
# Clipping below the ceiling keeps the bind in VARCHAR2 territory; the TO_CLOB()
# wrapper in upsert_sql() then makes the select-list type explicit.
CLOB_BIND_MAX_BYTES = int(os.getenv("IPO_JSON_MAX_BYTES", "30000"))


def _clip_utf8(value: str, max_bytes: int) -> str:
    """Trim a str so its UTF-8 encoding fits max_bytes, never splitting a char."""
    if value is None:
        return value
    b = value.encode("utf-8")
    if len(b) <= max_bytes:
        return value
    return b[:max_bytes].decode("utf-8", "ignore")

ROW_KEYS = (
    "symbol", "company_name", "issue_price", "listing_date",
    "listing_date_estimated", "issue_start_date", "issue_end_date", "stage",
    "nse_status", "subscription_times", "gmp", "source", "ipo_score",
    "decision", "buy_suggestion_json",
)


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if "channel_binding=" in url:
        url = re.sub(r"([&?])channel_binding=[^&]*", r"\1", url)
        url = url.replace("?&", "?").rstrip("?&")
    url = re.sub(r"(?i)([?&]sslmode=)required\b", r"\1require", url)
    if "sslmode=" not in url.lower():
        url = url + ("&" if "?" in url else "?") + "sslmode=require"
    return url


def is_oracle() -> bool:
    if _oc is not None:
        try:
            return _oc.oracle_is_configured(_raw_url() or "")
        except Exception:
            pass
    return bool(os.environ.get("ORACLE_DSN"))


def dialect() -> str:
    return "oracle" if is_oracle() else "postgresql"


def _raw_url() -> str:
    return (
        os.getenv("CACHE_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("TRAINING_DATABASE_URL")
        or ""
    )


def database_url() -> Optional[str]:
    url = _raw_url()
    if is_oracle():
        return url if url.lower().startswith("oracle") else "oracle+oracledb://"
    return _normalize_db_url(url) if url else None


def ddl_statements(dial: Optional[str] = None) -> list:
    dial = dial or dialect()
    return DDL_STATEMENTS_ORACLE if dial == "oracle" else DDL_STATEMENTS


def make_engine(app_name: str = "stockky-ipo"):
    url = database_url()
    if not url:
        return None
    from sqlalchemy import create_engine

    if is_oracle():
        if _oc is None:
            logger.warning("ipo: ORACLE_DSN set but oracle_compat.py missing")
            return None
        eng, _ = _oc.build_oracle_engine(
            url,
            db_pool_size=os.getenv("IPO_DB_POOL_SIZE", "2"),
            db_max_overflow=os.getenv("IPO_DB_MAX_OVERFLOW", "1"),
            db_pool_recycle=os.getenv("IPO_DB_POOL_RECYCLE", "300"),
            db_pool_timeout=os.getenv("IPO_DB_POOL_TIMEOUT", "15"),
        )
        return eng

    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=1,
        pool_timeout=8,
        connect_args={"connect_timeout": 8, "application_name": app_name},
    )


def table_exists_sql(dial: Optional[str] = None) -> str:
    dial = dial or dialect()
    if dial == "oracle":
        return "SELECT COUNT(*) FROM user_tables WHERE table_name = UPPER(:tbl)"
    return (
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = :tbl"
    )


def upsert_sql(dial: Optional[str] = None) -> str:
    dial = dial or dialect()
    if dial == "oracle":
        return """
            MERGE INTO ipo_static_feed d
            USING (
                SELECT :symbol AS symbol, :company_name AS company_name,
                       :issue_price AS issue_price,
                       TO_DATE(SUBSTR(:listing_date, 1, 10), 'YYYY-MM-DD') AS listing_date,
                       :listing_date_estimated AS listing_date_estimated,
                       :issue_start_date AS issue_start_date,
                       :issue_end_date AS issue_end_date,
                       :stage AS stage, :nse_status AS nse_status,
                       :subscription_times AS subscription_times, :gmp AS gmp,
                       :source AS source, :ipo_score AS ipo_score,
                       :decision AS decision,
                       TO_CLOB(:buy_suggestion_json) AS buy_suggestion_json
                FROM dual
            ) s ON (d.symbol = s.symbol)
            WHEN MATCHED THEN UPDATE SET
                d.company_name = s.company_name,
                d.issue_price = s.issue_price,
                d.listing_date = s.listing_date,
                d.listing_date_estimated = s.listing_date_estimated,
                d.issue_start_date = s.issue_start_date,
                d.issue_end_date = s.issue_end_date,
                d.stage = s.stage,
                d.nse_status = s.nse_status,
                d.subscription_times = s.subscription_times,
                d.gmp = s.gmp,
                d.source = s.source,
                d.ipo_score = s.ipo_score,
                d.decision = s.decision,
                d.buy_suggestion_json = s.buy_suggestion_json,
                d.updated_at = SYSTIMESTAMP
            WHEN NOT MATCHED THEN INSERT
                (symbol, company_name, issue_price, listing_date,
                 listing_date_estimated, issue_start_date, issue_end_date,
                 stage, nse_status, subscription_times, gmp, source,
                 ipo_score, decision, buy_suggestion_json, updated_at)
            VALUES
                (s.symbol, s.company_name, s.issue_price, s.listing_date,
                 s.listing_date_estimated, s.issue_start_date, s.issue_end_date,
                 s.stage, s.nse_status, s.subscription_times, s.gmp, s.source,
                 s.ipo_score, s.decision, s.buy_suggestion_json, SYSTIMESTAMP)
        """
    return """
        INSERT INTO ipo_static_feed
            (symbol, company_name, issue_price, listing_date,
             listing_date_estimated, issue_start_date, issue_end_date, stage,
             nse_status, subscription_times, gmp, source, ipo_score, decision,
             buy_suggestion_json, updated_at)
        VALUES
            (:symbol, :company_name, :issue_price,
             CASE WHEN :listing_date = '' THEN NULL ELSE :listing_date::date END,
             :listing_date_estimated, :issue_start_date, :issue_end_date, :stage,
             :nse_status, :subscription_times, :gmp, :source, :ipo_score, :decision,
             :buy_suggestion_json, NOW())
        ON CONFLICT (symbol) DO UPDATE SET
            company_name = EXCLUDED.company_name,
            issue_price = EXCLUDED.issue_price,
            listing_date = EXCLUDED.listing_date,
            listing_date_estimated = EXCLUDED.listing_date_estimated,
            issue_start_date = EXCLUDED.issue_start_date,
            issue_end_date = EXCLUDED.issue_end_date,
            stage = EXCLUDED.stage,
            nse_status = EXCLUDED.nse_status,
            subscription_times = EXCLUDED.subscription_times,
            gmp = EXCLUDED.gmp,
            source = EXCLUDED.source,
            ipo_score = EXCLUDED.ipo_score,
            decision = EXCLUDED.decision,
            buy_suggestion_json = EXCLUDED.buy_suggestion_json,
            updated_at = NOW()
    """


def adapt_rows(rows: list, dial: Optional[str] = None) -> list:
    """Make bind values safe for the target driver. Postgres path untouched.

    Oracle-only adjustments:
      * listing_date_estimated -> 1/0 (Oracle has no BOOLEAN; column is NUMBER(1)
        and python-oracledb refuses to bind a Python bool into it).
      * listing_date -> a real None when absent, so the MERGE's
        TO_DATE(SUBSTR(:listing_date,1,10),'YYYY-MM-DD') evaluates to NULL.
        This replaces an earlier '1900-01-01' sentinel: that wrote a real date
        into the column, so "no listing date yet" became indistinguishable from
        a genuine 1900 listing and every reader had to special-case it. Binding
        None is safe here because the Oracle writer executes row-by-row (see
        _ipo_db_upsert in ipo_scanner.py), so bind types are re-derived per
        statement — with executemany, oracledb would size the bind from the
        first row and a leading None would truncate later real dates.
      * VARCHAR2 columns are byte-limited, not char-limited. One non-ASCII
        character in a company name is 3 bytes in AL32UTF8, so a value that fits
        Postgres VARCHAR(160) can still raise ORA-12899 and kill the row. Trim
        to the declared byte budget instead of losing the whole IPO.
    """
    dial = dial or dialect()
    out = []
    for r in rows:
        r2 = dict(r)
        if dial == "oracle":
            if "listing_date_estimated" in r2 and r2["listing_date_estimated"] is not None:
                r2["listing_date_estimated"] = 1 if r2["listing_date_estimated"] else 0
            if not r2.get("listing_date"):
                r2["listing_date"] = None  # -> TO_DATE(NULL, ...) -> NULL
            # Keep the CLOB bind under Oracle's 32767-byte VARCHAR2 bind ceiling
            # so python-oracledb never escalates to DB_TYPE_LONG (see
            # CLOB_BIND_MAX_BYTES). Oracle-branch only: the Postgres path is
            # already-tested code and TEXT has no equivalent bind limit, so it
            # stays byte-for-byte as it was.
            bsj = r2.get("buy_suggestion_json")
            if isinstance(bsj, str):
                r2["buy_suggestion_json"] = _clip_utf8(bsj, CLOB_BIND_MAX_BYTES)
            for col, limit in _ORACLE_VARCHAR2_BYTES.items():
                v = r2.get(col)
                if isinstance(v, str):
                    r2[col] = _clip_utf8(v, limit)
        else:
            if "listing_date_estimated" not in r2:
                r2["listing_date_estimated"] = False
        out.append(r2)
    return out


def _heal_legacy_null_dates(eng) -> None:
    """Oracle only: rewrite the retired '1900-01-01' sentinel to a real NULL.

    Idempotent and best-effort — runs inside ensure_ipo_schema() (called before
    every scan), so a backup-production DB that already has sentinel rows heals
    itself on the next deploy without a manual migration. Never touches Postgres,
    where NULL was always stored correctly.
    """
    try:
        from sqlalchemy import text

        with eng.begin() as conn:
            res = conn.execute(
                text(
                    "UPDATE ipo_static_feed SET listing_date = NULL "
                    "WHERE listing_date = TO_DATE(:sentinel, 'YYYY-MM-DD')"
                ),
                {"sentinel": _LEGACY_NULL_DATE_SENTINEL},
            )
            n = getattr(res, "rowcount", 0) or 0
        if n:
            logger.info("ipo_static_feed: healed %s legacy sentinel listing_date row(s)", n)
    except Exception as e:  # table may not exist yet on a first-ever run
        logger.debug("ipo listing_date heal skipped: %s", str(e)[:160])


def ensure_ipo_schema() -> dict:
    url = database_url()
    if not url:
        return {"ok": False, "error": "No DATABASE_URL / CACHE_DATABASE_URL configured"}
    eng = None
    try:
        from sqlalchemy import text

        dial = dialect()
        eng = make_engine("stockky-ipo-schema")
        if eng is None:
            return {"ok": False, "error": "Could not build a database engine"}

        if dial == "oracle":
            for stmt in ddl_statements(dial):
                s = stmt.strip()
                if s and _oc is not None:
                    _oc.exec_ddl_safe(eng, s, "oracle")
            _heal_legacy_null_dates(eng)
        else:
            with eng.begin() as conn:
                for stmt in ddl_statements(dial):
                    s = stmt.strip()
                    if s:
                        conn.execute(text(s))

        logger.info("ipo_static_feed schema ready (backend=%s)", dial)
        return {"ok": True, "table": TABLE_NAME, "backend": dial}
    except Exception as e:
        logger.warning("ensure_ipo_schema failed: %s", e)
        return {"ok": False, "error": str(e)[:240]}
    finally:
        if eng is not None:
            try:
                eng.dispose()
            except Exception:
                pass
