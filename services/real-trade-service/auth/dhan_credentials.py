"""
auth/dhan_credentials.py — Layer 2 auth (the linked Dhan account).

Encrypts the Dhan client ID + access token at rest with Fernet
(config.DHAN_CREDENTIAL_ENC_KEY) and stores only the encrypted blobs plus a
masked display string in trade_credentials. The plaintext token is decrypted
ONLY inside execution/dhan_client.py, immediately before an API call — it is
never returned to the frontend again after the initial save (main.py's
connect-Dhan route returns only the masked id + connection status).

Decision 4: manual daily token paste is the default (DHAN_TOTP_ENABLED=false).
is_token_valid() is what the gate-state machine in main.py polls to decide
whether to auto-disarm on expiry; refresh_if_totp_enabled() is the opt-in
auto-refresh path for later.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

import config
import models

logger = logging.getLogger("real-trade-dhan-auth")

DHAN_TOKEN_LIFETIME_HOURS = config.DHAN_TOKEN_LIFETIME_DAYS * 24  # see config.py


def _fernet() -> Fernet:
    if not config.DHAN_CREDENTIAL_ENC_KEY:
        raise RuntimeError("DHAN_CREDENTIAL_ENC_KEY not configured on this deploy.")
    # os.getenv always returns str (or the "" default caught above), so this
    # never needs the bytes branch — kept simple rather than a defensive
    # isinstance check that could never actually take the other path.
    return Fernet(config.DHAN_CREDENTIAL_ENC_KEY.encode())


def _mask(client_id: str) -> str:
    client_id = (client_id or "").strip()
    if len(client_id) <= 4:
        return "*" * len(client_id)
    return "*" * (len(client_id) - 4) + client_id[-4:]


def save_credentials(db: Session, client_id: str, access_token: str) -> models.TradeCredential:
    """Encrypt and persist a freshly-pasted Dhan client id + access token.
    Called only from an admin-authenticated route. Overwrites the single
    existing row (there is exactly one trade_credentials row — this service
    supports one linked Dhan account)."""
    f = _fernet()
    now = datetime.now(timezone.utc)
    row = db.query(models.TradeCredential).first()
    if row is None:
        row = models.TradeCredential()
        db.add(row)
    row.dhan_client_id_masked = _mask(client_id)
    row.dhan_client_id_encrypted = f.encrypt(client_id.encode()).decode()
    row.access_token_encrypted = f.encrypt(access_token.encode()).decode()
    row.token_issued_at = now
    row.token_expires_at = now + timedelta(hours=DHAN_TOKEN_LIFETIME_HOURS)
    row.updated_at = now
    db.commit()
    db.refresh(row)
    return row


def get_decrypted_credentials(db: Session) -> Optional[tuple[str, str]]:
    """Returns (client_id, access_token) plaintext — ONLY for use inside
    execution/dhan_client.py, immediately before an API call. Returns None
    if nothing is stored or decryption fails (e.g. key rotated without
    re-saving credentials — fail closed, don't half-trust a bad decrypt)."""
    row = db.query(models.TradeCredential).first()
    if row is None or not row.access_token_encrypted:
        return None
    try:
        f = _fernet()
        client_id = f.decrypt(row.dhan_client_id_encrypted.encode()).decode()
        token = f.decrypt(row.access_token_encrypted.encode()).decode()
        return client_id, token
    except InvalidToken:
        logger.error("Dhan credential decrypt failed — encryption key mismatch or corrupted row.")
        return None


def connection_status(db: Session) -> dict:
    """Masked, frontend-safe status — this is the only shape of Dhan
    credential info that should ever cross the API boundary."""
    row = db.query(models.TradeCredential).first()
    if row is None or not row.access_token_encrypted:
        return {
            "connected": False,
            "client_id_masked": None,
            "token_expires_at": None,
            "token_valid": False,
            "days_remaining": None,
        }
    days_remaining = None
    if row.token_expires_at:
        delta = row.token_expires_at - datetime.now(timezone.utc)
        days_remaining = round(delta.total_seconds() / 86400, 1)
    return {
        "connected": True,
        "client_id_masked": row.dhan_client_id_masked,
        "token_expires_at": row.token_expires_at.isoformat() if row.token_expires_at else None,
        "token_valid": is_token_valid(row),
        "days_remaining": days_remaining,
    }


def is_token_valid(row: models.TradeCredential) -> bool:
    if row is None or not row.token_expires_at:
        return False
    return datetime.now(timezone.utc) < row.token_expires_at


def refresh_if_totp_enabled(db: Session) -> bool:
    """Opt-in auto-refresh path (decision 4). No-ops and returns False
    unless config.DHAN_TOTP_ENABLED is set — in the default manual-paste
    mode, an expired token is surfaced to the admin (and auto-disarms
    trading) rather than silently retried. Implementing the actual TOTP
    call is deferred until DHAN_TOTP_ENABLED is actually turned on for a
    real account, since it needs a live TOTP secret to test against —
    wiring it blind here would be untestable and is exactly the kind of
    "looks done but was never actually exercised" code this project is
    trying to avoid. See DhanHQ's /app/generateAccessToken docs."""
    if not config.DHAN_TOTP_ENABLED:
        return False
    logger.warning(
        "DHAN_TOTP_ENABLED=true but auto-refresh is not yet implemented — "
        "falling back to manual reauthentication. Wire this in execution/dhan_client.py "
        "once a real TOTP-enabled Dhan account is available to test against."
    )
    return False
