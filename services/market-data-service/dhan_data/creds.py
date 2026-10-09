"""dhan_data/creds.py - READ-ONLY access to the Dhan token real-trade-service owns (group 270).

Both services use the SAME Dhan account, so the token is pasted (or TOTP-refreshed) exactly once, in
real-trade-service, and stored encrypted in `trade_credentials`. This module only DECRYPTS and READS that row,
the same way position-stocks-service/auth/dhan_credentials_ro.py does. It has no save/refresh function on purpose:
two services refreshing one 24 h token would race each other.

Needs the same DHAN_CREDENTIAL_ENC_KEY as real-trade-service (a different key cannot decrypt the row).
The database is market-data-service's existing durable engine (kv_cache._get_neon): Oracle on the VM, Neon/Postgres
elsewhere. If real-trade-service uses a DIFFERENT database than the KV cache, set DHAN_CREDS_DATABASE_URL.
Nothing here ever logs or returns a token in an error string.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Tuple

from . import config
from .errors import DhanNotConfigured

logger = logging.getLogger("dhan-data.creds")

DHAN_HARD_CAP_HOURS = 24.0   # Dhan access tokens never live longer than 24 h (same constant as real-trade-service)

_SQL = ("SELECT dhan_client_id_encrypted, access_token_encrypted, token_issued_at, token_expires_at "
        "FROM trade_credentials ORDER BY id")

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "value": None, "reason": "not_loaded", "expires_at": None}
_NEG_TTL_S = 30.0

# Tests (and anyone with a separate credentials DB) can replace this: () -> SQLAlchemy engine or None.
_engine_provider: Optional[Callable[[], object]] = None
_own_engine = None


def _engine():
    global _own_engine
    if _engine_provider is not None:
        return _engine_provider()
    url = config.env_str("DHAN_CREDS_DATABASE_URL", "")
    if url:
        if _own_engine is None:
            from sqlalchemy import create_engine
            _own_engine = create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        return _own_engine
    from kv_cache import _get_neon
    return _get_neon()


def _as_text(v) -> Optional[str]:
    """Oracle returns CLOB columns as LOB objects; Postgres returns str."""
    if v is None:
        return None
    if hasattr(v, "read"):
        v = v.read()
    if isinstance(v, bytes):
        v = v.decode()
    v = str(v).strip()
    return v or None


def _aware(dt) -> Optional[datetime]:
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)   # real-trade-service stores naive UTC
    return dt


def _effective_expiry(issued, expires) -> Optional[datetime]:
    expires, issued = _aware(expires), _aware(issued)
    if expires is None:
        return None
    if issued is None:
        return expires
    return min(expires, issued + timedelta(hours=DHAN_HARD_CAP_HOURS))


def _load() -> Tuple[Optional[Tuple[str, str]], str, Optional[datetime]]:
    """Returns ((client_id, token) | None, reason, effective_expiry). Reason is a short machine code."""
    key = config.enc_key()
    if not key:
        return None, "enc_key_missing", None
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except Exception:  # noqa: BLE001
        return None, "cryptography_not_installed", None
    try:
        eng = _engine()
    except Exception as e:  # noqa: BLE001
        logger.warning("dhan creds: database engine unavailable: %s", type(e).__name__)
        return None, "db_unavailable", None
    if eng is None:
        return None, "db_unavailable", None
    try:
        from sqlalchemy import text
        with eng.connect() as conn:
            row = conn.execute(text(_SQL)).fetchone()
    except Exception as e:  # noqa: BLE001
        logger.warning("dhan creds: trade_credentials read failed: %s", type(e).__name__)
        return None, "db_read_failed", None
    if row is None:
        return None, "no_row", None
    cid_enc, tok_enc = _as_text(row[0]), _as_text(row[1])
    if not cid_enc or not tok_enc:
        return None, "not_connected", None
    expiry = _effective_expiry(row[2], row[3])
    try:
        f = Fernet(key.encode())
        client_id = f.decrypt(cid_enc.encode()).decode()
        token = f.decrypt(tok_enc.encode()).decode()
    except InvalidToken:
        return None, "key_mismatch", expiry
    except Exception:  # noqa: BLE001
        return None, "decrypt_failed", expiry
    if expiry is not None and datetime.now(timezone.utc) >= expiry:
        return None, "token_expired", expiry
    return (client_id, token), "ok", expiry


def get_credentials(force: bool = False) -> Tuple[str, str]:
    """(client_id, access_token) or raises DhanNotConfigured(reason). Cached; failures cached briefly."""
    now = time.monotonic()
    with _lock:
        age = now - _cache["at"]
        ttl = config.creds_cache_s() if _cache["value"] else _NEG_TTL_S
        if not force and _cache["at"] and age < ttl:
            if _cache["value"]:
                exp = _cache["expires_at"]
                if exp is None or datetime.now(timezone.utc) < exp:
                    return _cache["value"]
                # cached token expired meanwhile: reload
            else:
                raise DhanNotConfigured(_cache["reason"])
        value, reason, expiry = _load()
        _cache.update(at=now, value=value, reason=reason, expires_at=expiry)
        if value is None:
            raise DhanNotConfigured(reason)
        return value


def invalidate() -> None:
    """Forget the cached token (called after an auth error so the next call re-reads the row)."""
    with _lock:
        _cache.update(at=0.0, value=None, reason="not_loaded", expires_at=None)


def status() -> dict:
    """Frontend/ops-safe summary: never contains the token or client id."""
    try:
        get_credentials()
    except DhanNotConfigured:
        pass
    with _lock:
        exp = _cache["expires_at"]
        hours = None
        if exp is not None:
            hours = round((exp - datetime.now(timezone.utc)).total_seconds() / 3600.0, 2)
        return {
            "ok": bool(_cache["value"]),
            "reason": _cache["reason"],
            "token_expires_at": exp.isoformat() if exp else None,
            "hours_remaining": hours,
        }
