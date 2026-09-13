"""
auth/dhan_credentials_ro.py — READ-ONLY reader of the trade_credentials
row owned by real-trade-service.

Both services trade the SAME Dhan account/credentials, so the user should
never have to paste a token twice. But token *ownership* (saving, TOTP
auto-refresh, expiry tracking) stays exclusively with real-trade-service
— two services independently refreshing the same 24h-lifetime token would
race each other. This module can decrypt and read the current token; it
must NEVER write to trade_credentials. There is no save_credentials() or
refresh_if_totp_enabled() here on purpose — if you find yourself wanting
to add one, that's a sign that logic belongs in real-trade-service
instead, not here.

Uses its own lightweight, minimally-mapped read model for the same table
(rather than importing real-trade-service's models.py) to keep the two
services' SQLAlchemy metadata completely independent — see models.py's
module docstring.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.orm import Session, declarative_base

import config
from tz_utils import as_aware, iso_utc

logger = logging.getLogger("position-stocks-dhan-auth-ro")

# Dhan access tokens are HARD-CAPPED at 24h by Dhan/SEBI for every account,
# same constant as real-trade-service's auth/dhan_credentials.py — kept in
# sync there on purpose, duplicated here for the same isolation reasons as
# everything else in this file.
DHAN_HARD_CAP_HOURS = 24.0

_ROBase = declarative_base()


class TradeCredentialRO(_ROBase):
    """Read-only mirror of real-trade-service's trade_credentials table.
    Column set MUST stay in sync with real-trade-service/models.py's
    TradeCredential — if that table's schema changes, update this too.
    NEVER used with create_all()/init_tables() — this service does not
    own this table and must never create, alter, or drop it."""
    __tablename__ = "trade_credentials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    dhan_client_id_masked = Column(String(64), nullable=True)
    dhan_client_id_encrypted = Column(Text, nullable=True)
    access_token_encrypted = Column(Text, nullable=True)
    token_issued_at = Column(DateTime, nullable=True)
    token_expires_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False)


def _fernet() -> Fernet:
    if not config.DHAN_CREDENTIAL_ENC_KEY:
        raise RuntimeError(
            "DHAN_CREDENTIAL_ENC_KEY not configured — must match "
            "real-trade-service's key exactly, or decryption will fail."
        )
    return Fernet(config.DHAN_CREDENTIAL_ENC_KEY.encode())


def get_decrypted_credentials(db: Session) -> Optional[tuple[str, str]]:
    """Returns (client_id, access_token) plaintext, or None if nothing is
    stored / decryption fails / the key doesn't match real-trade-service's."""
    row = db.query(TradeCredentialRO).first()
    if row is None or not row.access_token_encrypted:
        return None
    try:
        f = _fernet()
        client_id = f.decrypt(row.dhan_client_id_encrypted.encode()).decode()
        token = f.decrypt(row.access_token_encrypted.encode()).decode()
        return client_id, token
    except InvalidToken:
        logger.error(
            "Dhan credential decrypt failed in position-stocks-service — "
            "DHAN_CREDENTIAL_ENC_KEY likely doesn't match real-trade-service's. "
            "Both services MUST share the exact same key (same Dhan account)."
        )
        return None


def is_connected(db: Session) -> bool:
    """Cheap check for /health and startup logging — does not decrypt."""
    row = db.query(TradeCredentialRO).first()
    return bool(row and row.access_token_encrypted)


def _effective_expiry(row: TradeCredentialRO):
    """token_expires_at, clamped to issued_at + DHAN_HARD_CAP_HOURS — verbatim
    logic mirror of real-trade-service's dhan_credentials._effective_expiry
    (the owning service's version is authoritative; this is a read-only
    copy for this service's own dashboard card)."""
    if row.token_expires_at is None:
        return None
    if row.token_issued_at is None:
        return as_aware(row.token_expires_at)
    hard_cap = as_aware(row.token_issued_at) + timedelta(hours=DHAN_HARD_CAP_HOURS)
    return min(as_aware(row.token_expires_at), hard_cap)


def connection_status(db: Session) -> dict:
    """Masked, frontend-safe Dhan connection/token status — read-only
    mirror of real-trade-service's dhan_credentials.connection_status().
    Both services show a "Dhan Account" card off the SAME shared account,
    so this deliberately returns the identical shape (client_id_masked,
    token_expires_at, days/hours/seconds_remaining, etc.) real-trade-
    service's frontend already renders, rather than inventing a second
    shape the frontend would need a second code path for. Never mutates
    trade_credentials — see module docstring."""
    row = db.query(TradeCredentialRO).first()
    if row is None or not row.access_token_encrypted:
        return {
            "connected": False,
            "client_id_masked": None,
            "token_issued_at": None,
            "token_expires_at": None,
            "token_valid": False,
            "token_hard_cap_hours": DHAN_HARD_CAP_HOURS,
            "days_remaining": None,
            "hours_remaining": None,
            "seconds_remaining": None,
        }
    days_remaining = hours_remaining = seconds_remaining = None
    effective_expiry = _effective_expiry(row)
    if effective_expiry:
        delta = effective_expiry - datetime.now(timezone.utc)
        seconds_remaining = max(0, round(delta.total_seconds()))
        days_remaining = round(delta.total_seconds() / 86400, 1)
        hours_remaining = round(delta.total_seconds() / 3600, 1)
    return {
        "connected": True,
        "client_id_masked": row.dhan_client_id_masked,
        "token_issued_at": iso_utc(row.token_issued_at) if row.token_issued_at else None,
        "token_expires_at": effective_expiry.isoformat() if effective_expiry else None,
        "token_valid": effective_expiry is not None and datetime.now(timezone.utc) < effective_expiry,
        "token_hard_cap_hours": DHAN_HARD_CAP_HOURS,
        "days_remaining": days_remaining,
        "hours_remaining": hours_remaining,
        "seconds_remaining": seconds_remaining,
    }
