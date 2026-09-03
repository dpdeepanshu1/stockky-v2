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

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

import config
import models
from tz_utils import as_aware, iso_utc

logger = logging.getLogger("real-trade-dhan-auth")

DHAN_TOKEN_LIFETIME_HOURS = config.DHAN_TOKEN_LIFETIME_DAYS * 24  # see config.py

# Dhan access tokens are HARD-CAPPED at 24h by Dhan/SEBI for every account —
# this is not configurable on Dhan's side no matter what
# DHAN_TOKEN_LIFETIME_DAYS is set to in this deploy's env. Every place that
# reads/derives an expiry from the DB clamps to this ceiling so a stale row
# (saved back when a misconfigured env had DHAN_TOKEN_LIFETIME_DAYS=30 — see
# CHANGES_2026-08-27_REVIEW.md #2) or a future misconfiguration can never
# show/enforce a countdown longer than Dhan itself will actually honor.
DHAN_HARD_CAP_HOURS = 24.0


def _effective_expiry(row: "models.TradeCredential"):
    """token_expires_at, clamped to issued_at + DHAN_HARD_CAP_HOURS. Falls
    back to the stored value if issued_at is missing (very old row)."""
    if row.token_expires_at is None:
        return None
    if row.token_issued_at is None:
        return as_aware(row.token_expires_at)
    hard_cap = as_aware(row.token_issued_at) + timedelta(hours=DHAN_HARD_CAP_HOURS)
    return min(as_aware(row.token_expires_at), hard_cap)


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


def save_credentials(
    db: Session,
    client_id: str,
    access_token: str,
    real_expires_at: Optional[datetime] = None,
) -> models.TradeCredential:
    """Encrypt and persist a freshly-pasted or freshly-regenerated Dhan
    client id + access token. Called only from an admin-authenticated route
    (manual paste) or refresh_if_totp_enabled() (TOTP auto-refresh).
    Overwrites the single existing row (there is exactly one
    trade_credentials row — this service supports one linked Dhan account).

    real_expires_at: when Dhan's own generateAccessToken response tells us
    the actual expiry (its `expiryTime` field), pass it here so the stored
    value reflects reality instead of the DHAN_TOKEN_LIFETIME_HOURS guess.
    Manual paste has no such data from Dhan, so it stays None there and
    falls back to the guess as before. Either way, _effective_expiry() still
    clamps to issued_at + DHAN_HARD_CAP_HOURS as a safety ceiling, so a bad
    or malformed value from Dhan can never make the displayed countdown
    LONGER than 24h — only shorter/more accurate.
    """
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
    row.token_expires_at = as_aware(real_expires_at) if real_expires_at else (now + timedelta(hours=DHAN_TOKEN_LIFETIME_HOURS))
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
            "hours_remaining": None,
            "seconds_remaining": None,
        }
    days_remaining = None
    hours_remaining = None
    seconds_remaining = None
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
        "token_valid": is_token_valid(row),
        "token_hard_cap_hours": DHAN_HARD_CAP_HOURS,
        "days_remaining": days_remaining,
        "hours_remaining": hours_remaining,
        "seconds_remaining": seconds_remaining,  # frontend ticks this down live, same as Dhan's own "Xh Ym" display
    }


def is_token_valid(row: models.TradeCredential) -> bool:
    if row is None or not row.token_expires_at:
        return False
    effective_expiry = _effective_expiry(row)
    return effective_expiry is not None and datetime.now(timezone.utc) < effective_expiry


def enforce_live_token(db: Session, mode: str = "REAL") -> tuple[bool, Optional[str]]:
    """Real-time counterpart to is_token_valid()'s local-clock math: actually
    asks Dhan whether the token still works right now, and disarms
    immediately if Dhan itself rejects it — not just when our own
    issued_at + DHAN_TOKEN_LIFETIME_DAYS countdown runs out.

    Why both checks exist: the local timer is correct (Dhan tokens are a
    strict 24h) but can't see the cases where Dhan invalidates a token
    EARLY — the admin generating a new token from Dhan Web immediately
    kills the old one, clock drift, or a Dhan-side revocation. Without
    this, entry_engine/exit_engine would keep trying to place REAL orders
    with a dead token every cycle, each one failing silently into a log
    line, while the dashboard still shows 🟢 armed.

    A generic/transient Dhan error (rate limit, brief outage) is
    deliberately NOT treated as a dead token here — only a message
    matching dhan_client.is_auth_error() disarms, so a bad second from
    Dhan's side never gets mistaken for an actually-expired credential.

    Local import of execution.dhan_client to avoid a circular import
    (dhan_client already imports this module for credentials)."""
    from execution import dhan_client  # noqa: PLC0415 — see docstring

    import models  # noqa: PLC0415

    ok, err = dhan_client.verify_token_live(db)
    if ok:
        return True, None
    if not dhan_client.is_auth_error(err or ""):
        return True, None  # transient/unknown Dhan error — don't disarm on a guess

    gate = db.query(models.TradeGateState).filter_by(mode=mode).first()
    if gate is not None:
        gate.dhan_connected = False
        was_armed = gate.armed
        gate.armed = False
        gate.disarmed_reason = f"Dhan rejected the token on a live check: {(err or '')[:200]}"
        gate.updated_at = datetime.now(timezone.utc)
        db.commit()
        if was_armed:
            logger.warning("REAL auto-disarmed — Dhan live check rejected the token: %s", err)
    return False, err


def disarm_on_invalid_ip(db: Session, mode: str, err: str) -> bool:
    """Companion to enforce_live_token, for the OTHER category of
    persistent (not transient) Dhan rejection: the host's outbound IP
    isn't on Dhan's order-placement allowlist (see
    execution.dhan_client.is_invalid_ip_error's docstring for why this is
    a separate failure mode from an expired token — reads still work,
    only place/modify/cancel are IP-gated).

    Called from entry_engine/exit_engine/manual_engine's own except blocks
    the moment a placement fails with this specific error — waiting for
    the next cycle's enforce_live_token wouldn't catch it (that check uses
    a read-only call, which isn't IP-gated and will keep reporting "token
    ok"). Every order this cycle and every cycle after would otherwise
    fail identically until a human fixes the IP allowlist, so this stops
    the bleeding immediately rather than retrying a doomed request
    candidate-by-candidate, cycle after cycle.

    Returns True if it actually disarmed (was armed before this call) —
    callers use this to decide whether the loud "auto-paused" alert is
    warranted or whether this is just confirming an already-disarmed state.
    """
    import models  # noqa: PLC0415

    gate = db.query(models.TradeGateState).filter_by(mode=mode).first()
    if gate is None:
        return False
    was_armed = gate.armed
    gate.armed = False
    gate.disarmed_reason = (
        f"Dhan rejected the order — outbound IP not whitelisted: {(err or '')[:200]} "
        f"(check GET /dhan/network-check for this service's current outbound IP, "
        f"then add it under Dhan Web → My Profile → API Access → IP Whitelisting)"
    )
    gate.updated_at = datetime.now(timezone.utc)
    db.commit()
    if was_armed:
        logger.warning("REAL auto-disarmed — Dhan rejected an order for an unwhitelisted IP: %s", err)
    return was_armed


def token_needs_refresh(db: Session) -> bool:
    """True when the current Dhan token is within
    config.DHAN_TOTP_REFRESH_MARGIN_HOURS of its effective (hard-capped)
    expiry, or there's no usable token at all yet.

    Referenced by execution/auto_pilot.py:_totp_refresh_loop but was
    missing from this module — every proactive-refresh tick threw
    ImportError (caught, logged, retried next tick — never crashed the
    loop, but also meant proactive refresh silently never ran). Added
    here alongside the config.py fix for the two DHAN_TOTP_REFRESH_*
    settings that same loop logs on startup.

    No live Dhan call here — purely the same local-clock math
    is_token_valid()/connection_status() already use, kept fast since the
    loop polls this every DHAN_TOTP_REFRESH_CHECK_INTERVAL_SECONDS.
    """
    row = db.query(models.TradeCredential).first()
    if row is None or not row.access_token_encrypted:
        return True  # nothing saved yet — let refresh_if_totp_enabled try
    effective_expiry = _effective_expiry(row)
    if effective_expiry is None:
        return True
    margin = timedelta(hours=config.DHAN_TOTP_REFRESH_MARGIN_HOURS)
    return datetime.now(timezone.utc) >= (effective_expiry - margin)


def _parse_dhan_expiry(raw) -> Optional[datetime]:
    """Best-effort parse of Dhan's `expiryTime` field from
    /app/generateAccessToken. Field format was never confirmed against a
    live response before 2026-09-03 (see refresh_if_totp_enabled's
    docstring) — this logs the raw value once so the format can be
    confirmed, and returns None on anything it can't parse so callers
    fall back to the existing DHAN_TOKEN_LIFETIME_HOURS guess rather than
    ever storing a wrong/garbage expiry.
    """
    if raw is None:
        return None
    logger.info("Dhan expiryTime raw value: %r (type %s)", raw, type(raw).__name__)
    try:
        if isinstance(raw, (int, float)):
            # Epoch seconds vs milliseconds — Dhan/most brokers use seconds,
            # but guard against ms (13-digit) just in case.
            ts = raw / 1000.0 if raw > 10_000_000_000 else float(raw)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        s = str(raw).strip()
        if not s:
            return None
        # Try ISO 8601 first (handles "...Z" and offset forms).
        #
        # CONFIRMED 2026-09-03 against a real Dhan response: the actual
        # expiryTime value is bare/naive ISO ('2026-09-04T11:11:11.614',
        # no offset) and is in IST, not UTC — same convention as the
        # "YYYY-MM-DD HH:MM:SS" branch below. The original version of
        # this branch called as_aware() on the naive parsed datetime,
        # which (per tz_utils.as_aware's own contract) stamps naive
        # values as UTC — silently wrong by +5:30 for every Dhan expiry.
        # It didn't show up as a visible symptom before because the 24h
        # _effective_expiry safety-cap happened to land on the same
        # instant for a straight-24h token; a shorter-lived token would
        # have made the app think it had far more time left than it
        # really did. Only a string that already carries an explicit
        # offset/"Z" is trusted as directly UTC-convertible, as-is.
        try:
            parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                ist = parsed.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
                return ist.astimezone(timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
        # Common Dhan-style "YYYY-MM-DD HH:MM:SS" (assume IST, per Dhan's
        # docs convention — convert to UTC).
        try:
            naive = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            ist = naive.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
            return ist.astimezone(timezone.utc)
        except ValueError:
            pass
        logger.error("Could not parse Dhan expiryTime value: %r — falling back to lifetime guess.", raw)
        return None
    except Exception:
        logger.exception("Unexpected error parsing Dhan expiryTime %r — falling back to lifetime guess.", raw)
        return None


def refresh_if_totp_enabled(db: Session) -> bool:
    """
    §2 — TOTP auto-refresh for Dhan access token.

    Calls Dhan's /app/generateAccessToken endpoint with a fresh TOTP code.
    Enabled only when DHAN_TOTP_ENABLED=true (default: false — manual paste).

    FIELD NAME NOTE: Verify accessToken vs access_token against a live
    sandbox call. Code logs all response keys on first call for confirmation.

    Called by cycle_runner at the start of every REAL cycle, and by the
    notification-scheduler's TOTP refresh cron (every 12-20h).
    """
    if not config.DHAN_TOTP_ENABLED:
        return False

    totp_secret = os.environ.get("DHAN_TOTP_SECRET", "")
    client_id   = os.environ.get("DHAN_CLIENT_ID", "")
    dhan_pin    = os.environ.get("DHAN_PIN", "")

    if not totp_secret or not client_id:
        logger.error(
            "DHAN_TOTP_ENABLED=true but DHAN_TOTP_SECRET or DHAN_CLIENT_ID not set."
        )
        return False

    try:
        import pyotp
        totp_code = pyotp.TOTP(totp_secret).now()
        import httpx as _httpx
        r = _httpx.post(
            "https://auth.dhan.co/app/generateAccessToken",
            params={"dhanClientId": client_id, "pin": dhan_pin, "totp": totp_code},
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        # Log keys so field name can be verified against live sandbox response
        logger.info("Dhan TOTP refresh response keys: %s", list(data.keys()))
        new_token = (
            data.get("accessToken")
            or data.get("access_token")
            or (data.get("data") or {}).get("accessToken")
            or (data.get("data") or {}).get("access_token")
        )
        if not new_token:
            logger.error(
                "refresh_if_totp_enabled: no token field in response. Raw: %s", data
            )
            return False
        real_expiry = _parse_dhan_expiry(
            data.get("expiryTime") or (data.get("data") or {}).get("expiryTime")
        )
        save_credentials(db, client_id, new_token, real_expires_at=real_expiry)
        logger.info(
            "Dhan TOTP token refreshed successfully. Real expiry from Dhan: %s",
            real_expiry.isoformat() if real_expiry else "unparsed — used 24h lifetime guess",
        )

        # BUG FIX (2026-09-01): a fresh token landing here fixed the
        # CREDENTIAL but, if REAL had already auto-disarmed with
        # dhan_connected=False (main.py's gate-expiry check / enforce_live_token
        # rejecting the old token), left the gate flag stuck False — /arm kept
        # 409'ing on "missing gates: dhan_connected" until a human re-opened
        # the dashboard and re-saved the same token manually. Heal the gate
        # flag here so /arm stops being blocked; REAL still stays disarmed —
        # the admin still re-arms explicitly, this only clears the stale flag.
        try:
            gate = db.query(models.TradeGateState).filter_by(mode="REAL").first()
            if gate is not None and not gate.dhan_connected:
                gate.dhan_connected = True
                gate.dhan_connected_at = datetime.now(timezone.utc)
                db.commit()
                logger.info("REAL gate.dhan_connected restored after TOTP refresh.")
        except Exception:
            logger.exception("Failed to restore gate.dhan_connected after TOTP refresh (non-fatal).")

        try:
            from notifier import notify_sync
            notify_sync("🔑 *Dhan TOTP token refreshed* — new token saved.")
        except Exception:
            pass
        return True
    except Exception as e:
        logger.error("refresh_if_totp_enabled failed: %s", e)
        try:
            from notifier import notify_sync
            notify_sync(f"🚨 *Dhan TOTP refresh FAILED*\n{str(e)[:300]}")
        except Exception:
            pass
        return False