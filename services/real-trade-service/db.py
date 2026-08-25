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
