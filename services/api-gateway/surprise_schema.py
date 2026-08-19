"""
Surprise static feed schema — Neon / Postgres.

Creates `surprise_static_feed` used by pre-market baseline job and
lightweight intraday surprise scanner.

Env: CACHE_DATABASE_URL | DATABASE_URL | TRAINING_DATABASE_URL
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger("surprise-schema")

DDL = """
CREATE TABLE IF NOT EXISTS surprise_static_feed (
    symbol VARCHAR(30) PRIMARY KEY,
    prev_close NUMERIC(12, 2) NOT NULL,
    avg_15m_volume BIGINT NOT NULL DEFAULT 10000,
    daily_atr NUMERIC(12, 2) NOT NULL DEFAULT 0.0,
    high_52w NUMERIC(12, 2) NOT NULL,
    dist_52w_pct NUMERIC(8, 2) NOT NULL,
    sector VARCHAR(80),
    is_liquid BOOLEAN DEFAULT TRUE,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_surprise_static_sym ON surprise_static_feed(symbol);
CREATE INDEX IF NOT EXISTS idx_surprise_static_updated ON surprise_static_feed(updated_at);
"""


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
    url = (
        os.getenv("CACHE_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("TRAINING_DATABASE_URL")
    )
    return _normalize_db_url(url) if url else None


def ensure_surprise_schema() -> dict:
    """Create table/indexes if missing. Safe to call on every premarket run."""
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
            for stmt in DDL.strip().split(";"):
                s = stmt.strip()
                if s:
                    conn.execute(text(s))
        eng.dispose()
        logger.info("surprise_static_feed schema ready")
        return {"ok": True, "table": "surprise_static_feed"}
    except Exception as e:
        logger.warning("ensure_surprise_schema failed: %s", e)
        return {"ok": False, "error": str(e)[:240]}
