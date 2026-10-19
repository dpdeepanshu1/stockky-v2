"""
auth/admin_override.py - group 460: an OPTIONAL admin password changed from the page ("Change password" on the login screen).

The admin password is an Argon2id hash in the ADMIN_PASSWORD_HASH env var, read separately by real-trade-service and position-stocks-service.
A web page cannot edit an env var, so a changed password is stored as ONE row in a table both services share (same physical DB, same table name,
same columns - models.SharedAdminCredential) and both check it at login.

Safety rules (this sits in front of real money, so each is deliberate):
  * OFF unless ADMIN_PASSWORD_CHANGE_ENABLED=true in THIS service's environment. Off = nothing here is read, login is the old env-hash check.
  * Any problem reading the row (table missing, DB down, bad data) returns None, so login falls back to the env hash. It never locks you out by failing.
  * Lock-out recovery: set ADMIN_IGNORE_PASSWORD_OVERRIDE=true and restart; the env hash is used again and the stored row is ignored.
  * Once an override exists the old env password stops working (otherwise a "change" would change nothing).
  * Changing needs a valid admin session AND the current password, a minimum length, and is rate limited (5 wrong attempts / 10 minutes).
  * The username is not changeable here (ADMIN_USERNAME stays an env setting).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import List, Optional

import config

logger = logging.getLogger("admin-override-ps")

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128
ROW_KEY = "admin"
_MAX_WRONG = 5
_WINDOW_S = 600

_lock = threading.Lock()
_wrong: List[float] = []


class ChangeRejected(Exception):
    """Carries the HTTP status and a user-safe message."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def enabled() -> bool:
    return bool(getattr(config, "ADMIN_PASSWORD_CHANGE_ENABLED", False)) and not bool(getattr(config, "ADMIN_IGNORE_PASSWORD_OVERRIDE", False))


def _session():
    import db as _db  # local import: keeps this module importable without a DB
    return _db.get_session_factory()()


def stored_hash() -> Optional[str]:
    """The override hash, or None (feature off / no row / ANY error) -> caller uses the env hash."""
    if not enabled():
        return None
    s = None
    try:
        import models
        s = _session()
        row = s.get(models.SharedAdminCredential, ROW_KEY)
        h = (row.password_hash or "").strip() if row is not None else ""
        if not h or not h.startswith("$argon2"):
            return None
        return h
    except Exception as e:  # noqa: BLE001 - never lock the admin out because the lookup failed
        logger.warning("admin override lookup failed, using the env hash: %s", e)
        return None
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass


def _check_new_password(new_password: str, current_password: str) -> None:
    if not isinstance(new_password, str) or len(new_password) < MIN_PASSWORD_LENGTH:
        raise ChangeRejected(422, f"New password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(new_password) > MAX_PASSWORD_LENGTH:
        raise ChangeRejected(422, f"New password must be at most {MAX_PASSWORD_LENGTH} characters.")
    if new_password == current_password:
        raise ChangeRejected(422, "New password must be different from the current one.")
    if new_password.strip() != new_password:
        raise ChangeRejected(422, "New password must not start or end with a space.")


def _rate_limited() -> bool:
    now = time.time()
    with _lock:
        _wrong[:] = [t for t in _wrong if now - t < _WINDOW_S]
        return len(_wrong) >= _MAX_WRONG


def _note_wrong() -> None:
    with _lock:
        _wrong.append(time.time())


def reset_rate_limit() -> None:  # tests
    with _lock:
        _wrong.clear()


def change_password(username: str, current_password: str, new_password: str, verify) -> None:
    """Verify the current password (via `verify(username, password) -> bool`), then store the new hash. Raises ChangeRejected."""
    if not enabled():
        raise ChangeRejected(403, "Changing the password from the page is switched off (ADMIN_PASSWORD_CHANGE_ENABLED).")
    if _rate_limited():
        raise ChangeRejected(429, "Too many wrong attempts. Try again in a few minutes.")
    if not verify(username, current_password):
        _note_wrong()
        raise ChangeRejected(401, "Current password is wrong.")
    _check_new_password(new_password, current_password)
    from argon2 import PasswordHasher
    new_hash = PasswordHasher().hash(new_password)
    import models
    s = _session()
    try:
        row = s.get(models.SharedAdminCredential, ROW_KEY)
        now = datetime.now(timezone.utc)
        if row is None:
            s.add(models.SharedAdminCredential(key=ROW_KEY, username=username, password_hash=new_hash, updated_at=now))
        else:
            row.username = username
            row.password_hash = new_hash
            row.updated_at = now
        s.commit()
    except Exception as e:  # noqa: BLE001
        s.rollback()
        logger.error("admin override save failed: %s", e)
        raise ChangeRejected(503, "Could not save the new password (database unavailable). The old password still works.")
    finally:
        s.close()
    reset_rate_limit()
    logger.info("admin password changed from the page (override row updated)")
