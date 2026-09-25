"""
Upstash QStash client — schedule HTTP callbacks without Redis command burn.

Env:
  QSTASH_TOKEN, QSTASH_CURRENT_SIGNING_KEY, QSTASH_NEXT_SIGNING_KEY, API_GATEWAY_URL
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("qstash")

QSTASH_URL = os.environ.get("QSTASH_URL", "https://qstash.upstash.io/v2/publish")
QSTASH_TOKEN = os.environ.get("QSTASH_TOKEN", "").strip()
SIGN_CURRENT = os.environ.get("QSTASH_CURRENT_SIGNING_KEY", "").strip()
SIGN_NEXT = os.environ.get("QSTASH_NEXT_SIGNING_KEY", "").strip()


def enabled() -> bool:
    return bool(QSTASH_TOKEN)


def publish(
    destination_url: str,
    body: Optional[Dict[str, Any]] = None,
    *,
    delay_seconds: int = 0,
    retries: int = 2,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    if not QSTASH_TOKEN:
        return {"ok": False, "error": "QSTASH_TOKEN not set"}
    dest = destination_url.strip()
    if not dest.startswith("http"):
        return {"ok": False, "error": f"invalid destination: {dest}"}
    url = f"{QSTASH_URL.rstrip('/')}/{dest}"
    hdrs = {
        "Authorization": f"Bearer {QSTASH_TOKEN}",
        "Content-Type": "application/json",
        "Upstash-Retries": str(max(0, min(retries, 5))),
    }
    if delay_seconds and delay_seconds > 0:
        hdrs["Upstash-Delay"] = f"{int(delay_seconds)}s"
    if headers:
        for k, v in headers.items():
            hdrs[f"Upstash-Forward-{k}"] = str(v)
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.post(url, json=body or {}, headers=hdrs)
            if r.status_code >= 400:
                logger.warning("QStash publish %s → %s %s", dest, r.status_code, r.text[:200])
                return {"ok": False, "status": r.status_code, "body": r.text[:300]}
            data = r.json() if r.content else {}
            logger.info("QStash published → %s messageId=%s", dest, data.get("messageId"))
            return {"ok": True, **data}
    except Exception as e:
        logger.warning("QStash publish failed: %s", e)
        return {"ok": False, "error": str(e)}


def schedule_gateway_tick(
    path: str = "/ops/qstash/tick",
    *,
    delay_seconds: int = 0,
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    base = (os.environ.get("API_GATEWAY_URL") or "").rstrip("/")
    if not base:
        return {"ok": False, "error": "API_GATEWAY_URL not set"}
    return publish(f"{base}{path}", body or {"source": "qstash"}, delay_seconds=delay_seconds)


def verify_signature(
    signature_header: str,
    body: bytes,
    expected_url: Optional[str] = None,
) -> bool:
    """Verify an Upstash-Signature JWT per QStash's own spec:
    https://upstash.com/docs/qstash/howto/signature — iss must be "Upstash",
    sub must match the destination URL the callback was sent to, exp must
    not have passed, and the body claim must match a SHA-256 hash of the
    raw request body. `expected_url` is optional (older/self-hosted
    deployments may not have API_GATEWAY_URL configured); when omitted, the
    sub claim is still required to be present (via `require`) but its value
    is not pinned.
    """
    if not SIGN_CURRENT and not SIGN_NEXT:
        logger.debug("QStash signature keys not set — accepting callback")
        return True
    if not signature_header:
        return False
    if len(signature_header.split(".")) != 3:
        return False
    try:
        import jwt  # type: ignore
    except ImportError:
        # Fail-open, same as every other degraded-dependency path in this
        # codebase (see shared_exposure.py) — but always log it, since a
        # missing PyJWT here means EVERY QStash callback is accepted
        # unverified until the dependency is installed.
        logger.warning(
            "PyJWT not installed — cannot verify QStash signatures; "
            "accepting callback unverified (fail-open). Install PyJWT to "
            "enable signature verification."
        )
        return True

    body_hash = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode("ascii").rstrip("=")
    keys = [k for k in (SIGN_CURRENT, SIGN_NEXT) if k]
    last_err: Any = None
    for key in keys:
        try:
            claims = jwt.decode(
                signature_header,
                key,
                algorithms=["HS256"],
                options={"require": ["iss", "sub", "exp"]},
            )
            if claims.get("iss") != "Upstash":
                last_err = f"unexpected iss claim: {claims.get('iss')!r}"
                continue
            if expected_url is not None and claims.get("sub") != expected_url:
                last_err = f"unexpected sub claim: {claims.get('sub')!r} (expected {expected_url!r})"
                continue
            claim_body_hash = (claims.get("body") or "").rstrip("=")
            if claim_body_hash and claim_body_hash != body_hash:
                last_err = "body hash mismatch"
                continue
            return True
        except Exception as e:
            last_err = e
    logger.warning("QStash JWT verify failed: %s", last_err)
    return False
