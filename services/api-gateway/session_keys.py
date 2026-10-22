"""session_keys.py - A4b: one signing secret per service instead of one SESSION_SECRET shared by all.

Admin tokens are HS256 JWTs. Before: every service signed and verified with the same SESSION_SECRET, so a leak from ANY of them
(gateway, calibration runner, ...) could forge an admin session everywhere. Now each service has its own secret and every token
carries a `kid` header naming the service that signed it:

    SESSION_SECRET_REAL_TRADE, SESSION_SECRET_POSITION_STOCKS, SESSION_SECRET_CALIBRATION_RUNNER

  * SIGN: a service signs with its own secret (kid = service id). If that is not set it falls back to the legacy SESSION_SECRET
    (no kid), so an existing deploy keeps working until the new values are filled in.
  * VERIFY: the `kid` picks the key. A service only accepts the kids in its own `accept` list (hard-coded by the caller, so a
    secret that reaches its environment through `env_file` is still ignored unless that service is meant to use it).
    A token with NO kid is a legacy token: it is checked against SESSION_SECRET only. Remove SESSION_SECRET and legacy tokens stop working.
  * An unknown kid, or a kid whose secret is not set here, is rejected. Nothing falls back to another key.

Limit worth knowing: HS256 is symmetric, so a service that can VERIFY a kid's tokens could also forge them. The gain is that a
leak is now limited to the services that hold that key, each issuer can be rotated or switched off alone, and every token says
who signed it. Real separation of "can sign" from "can verify" would need an asymmetric algorithm (not done here).

This file is identical in every service that has it (scripts/check_shared_copies.py enforces that).
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, Iterable, Optional

import jwt

SERVICE_ENV = {
    "real-trade": "SESSION_SECRET_REAL_TRADE",
    "position-stocks": "SESSION_SECRET_POSITION_STOCKS",
    "calibration-runner": "SESSION_SECRET_CALIBRATION_RUNNER",
}
MIN_SECRET_LEN = 32
ALGORITHM = "HS256"


class MissingSecret(Exception):
    """No usable signing secret (neither the service's own nor the legacy one)."""


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def own_secret(service: str) -> str:
    name = SERVICE_ENV.get(service)
    return _env(name) if name else ""


def can_sign(service: str, legacy: str = "") -> bool:
    return bool(own_secret(service) or (legacy or "").strip())


def sign(service: str, claims: Dict[str, Any], legacy: str = "") -> str:
    """Sign `claims` with the service's own secret (kid header), else the legacy secret (no kid)."""
    key = own_secret(service)
    if key:
        return jwt.encode(claims, key, algorithm=ALGORITHM, headers={"kid": service})
    legacy = (legacy or "").strip()
    if legacy:
        return jwt.encode(claims, legacy, algorithm=ALGORITHM)
    raise MissingSecret(f"no signing secret for {service}: set {SERVICE_ENV.get(service, 'SESSION_SECRET_<SERVICE>')} (or the legacy SESSION_SECRET)")


def _key_for(token: str, accept: Iterable[str], legacy: str) -> Optional[str]:
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.InvalidTokenError:
        return None
    if kid is None:
        return (legacy or "").strip() or None
    if isinstance(kid, str) and kid in SERVICE_ENV and kid in set(accept):
        return own_secret(kid) or None
    return None


def decode(token: str, accept: Iterable[str], legacy: str = "", options: Optional[dict] = None) -> Dict[str, Any]:
    """Return the verified claims. Raises jwt.InvalidTokenError (ExpiredSignatureError included) for anything not accepted."""
    key = _key_for(token, accept, legacy)
    if not key:
        raise jwt.InvalidTokenError("no key available for this token")
    return jwt.decode(token, key, algorithms=[ALGORITHM], options=options or {})


def _fp(secret: str) -> Optional[str]:
    return hashlib.sha256(secret.encode()).hexdigest()[:8] if secret else None


def diagnostics(service: str, accept: Iterable[str], legacy: str = "") -> Dict[str, Any]:
    """Non-secret snapshot (fingerprints only). Two services should show the SAME fingerprint for the same kid."""
    legacy = (legacy or "").strip()
    keys = {}
    for kid in [service] + [a for a in accept if a != service]:
        s = own_secret(kid)
        keys[kid] = {"configured": bool(s), "length": len(s), "fingerprint": _fp(s)}
    values = [own_secret(k) for k in SERVICE_ENV if own_secret(k)]
    return {
        "signing_mode": "per-service" if own_secret(service) else ("legacy-shared" if legacy else "none"),
        "own_kid": service,
        "accepts": keys,
        "legacy_secret_configured": bool(legacy),
        "legacy_fingerprint": _fp(legacy),
        "weak_secret": any(0 < len(v) < MIN_SECRET_LEN for v in values + ([legacy] if legacy else [])),
        "duplicate_secrets": len(values) != len(set(values)) or (bool(legacy) and legacy in values),
    }
