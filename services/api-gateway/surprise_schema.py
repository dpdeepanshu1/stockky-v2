"""
Surprise static feed schema — Neon / Postgres.

Single source of truth for table name + columns used by:
  - surprise_premarket.py (INSERT)
  - surprise_scanner.py (SELECT)

Table: surprise_static_feed
Columns:
  symbol, prev_close, avg_15m_volume, daily_atr, high_52w, dist_52w_pct,
  sector, is_liquid, updated_at

Env: CACHE_DATABASE_URL | DATABASE_URL | TRAINING_DATABASE_URL
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger("surprise-schema")

TABLE_NAME = "surprise_static_feed"

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

# Explicit column list — never rely on SELECT * for scanner logic
SELECT_COLUMNS = (
    "symbol, prev_close, avg_15m_volume, daily_atr, high_52w, "
    "dist_52w_pct, sector, is_liquid, updated_at"
)

INSERT_COLUMNS = (
    "symbol, prev_close, avg_15m_volume, daily_atr, high_52w, "
    "dist_52w_pct, sector, is_liquid, updated_at"
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


def database_url() -> Optional[str]:
    # Oracle Cloud side: the surprise static feed is Postgres-only and degrades
    # to a clean no-op (ensure_surprise_schema returns ok=False, callers already
    # handle that). Durable core runs on Oracle via ORACLE_DSN. On Render/Neon
    # this guard is False so behaviour is unchanged.
    if os.environ.get("ORACLE_DSN"):
        return None
    url = (
        os.getenv("CACHE_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("TRAINING_DATABASE_URL")
    )
    if url and url.lower().startswith("oracle"):
        return None
    return _normalize_db_url(url) if url else None


def ensure_surprise_schema() -> dict:
    """Create table/indexes if missing. Safe to call on every premarket / scan."""
    url = database_url()
    if not url:
        return {"ok": False, "error": "No DATABASE_URL / CACHE_DATABASE_URL configured"}
    try:
        from sqlalchemy import create_engine, text

        eng = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=1,
            max_overflow=1,
            pool_timeout=8,
            connect_args={"connect_timeout": 8, "application_name": "stockky-surprise-schema"},
        )
        with eng.begin() as conn:
            for stmt in DDL_STATEMENTS:
                s = stmt.strip()
                if s:
                    conn.execute(text(s))
        eng.dispose()
        logger.info("surprise_static_feed schema ready")
        return {"ok": True, "table": TABLE_NAME}
    except Exception as e:
        logger.warning("ensure_surprise_schema failed: %s", e)
        return {"ok": False, "error": str(e)[:240]}
