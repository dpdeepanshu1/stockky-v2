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
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.orm import Session, declarative_base

import config

logger = logging.getLogger("position-stocks-dhan-auth-ro")

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
