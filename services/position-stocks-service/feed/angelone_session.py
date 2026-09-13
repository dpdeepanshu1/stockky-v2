"""
feed/angelone_session.py — AngelOne TOTP-authenticated session for
position-stocks-service.

Duplicated from market-data-service/angelone_client.py (same isolation
rationale as execution/dhan_client.py — see config.py docstring). The
key additions over the market-data-service copy are:
  1. feed_token is surfaced prominently — the WS client (feed/ws_client.py)
     needs it for smartWebSocketV2 authentication.
  2. No rate-limiter dependency (market-data-service's rate_limiter.py is
     not importable here) — this service has its own loop and controls its
     own call rate organically.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx
import pyotp

import config

logger = logging.getLogger("position-stocks-ao-session")

_BASE = "https://apiconnect.angelone.in"

_outbound_ip_cache: dict = {"ip": None, "at": 0.0}
_OUTBOUND_IP_TTL_SECONDS = 15 * 60


def _get_outbound_ip() -> Optional[str]:
    try:
        resp = httpx.get("https://api.ipify.org?format=json", timeout=6.0)
        resp.raise_for_status()
        return resp.json().get("ip")
    except Exception as e:
        logger.warning("get_outbound_ip failed: %s", e)
        return None


def _resolve_client_public_ip() -> str:
    explicit = config.ANGELONE_STATIC_IP
    if explicit:
        return explicit
    now = time.time()
    cached_ip = _outbound_ip_cache["ip"]
    cached_at = _outbound_ip_cache["at"]
    if cached_ip and (now - cached_at) < _OUTBOUND_IP_TTL_SECONDS:
        return cached_ip
    detected = _get_outbound_ip()
    if detected:
        _outbound_ip_cache["ip"] = detected
        _outbound_ip_cache["at"] = now
        return detected
    logger.warning(
        "position-stocks: could not resolve real outbound IP — falling back to "
        "127.0.0.1, which AngelOne WILL reject on secure endpoints. "
        "Set ANGELONE_STATIC_IP in env."
    )
    return "127.0.0.1"


class AngelOneSession:
    """TOTP-authenticated AngelOne SmartAPI session.
    Exposes feed_token for WS auth."""

    def __init__(self) -> None:
        self.client_id   = config.ANGELONE_CLIENT_ID
        self.mpin        = config.ANGELONE_MPIN
        self.api_key     = config.ANGELONE_API_KEY
        self.totp_secret = config.ANGELONE_TOTP_SECRET
        self.token:        Optional[str]      = None
        self.feed_token:   Optional[str]      = None
        self.token_expiry: Optional[datetime] = None
        self._lock = asyncio.Lock()

    def is_configured(self) -> bool:
        return bool(self.client_id and self.mpin and self.api_key and self.totp_secret)

    async def ensure_session(self) -> None:
        """Refresh session if expired or missing. Async-safe via lock."""
        async with self._lock:
            if (
                self.token
                and self.token_expiry
                and datetime.utcnow() < self.token_expiry
            ):
                return
            await self._login()

    async def _login(self) -> None:
        if not self.is_configured():
            raise RuntimeError(
                "AngelOne not configured — set ANGELONE_CLIENT_ID, ANGELONE_MPIN, "
                "ANGELONE_API_KEY, ANGELONE_TOTP_SECRET env vars."
            )
        otp = pyotp.TOTP(self.totp_secret).now()
        client_public_ip = _resolve_client_public_ip()
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{_BASE}/rest/auth/angelbroking/user/v1/loginByPassword",
                json={
                    "clientcode": self.client_id,
                    "password":   self.mpin,
                    "totp":       otp,
                },
                headers={
                    "X-PrivateKey":      self.api_key,
                    "Content-Type":      "application/json",
                    "Accept":            "application/json",
                    "X-UserType":        "USER",
                    "X-SourceID":        "WEB",
                    "X-ClientLocalIP":   "127.0.0.1",
                    "X-ClientPublicIP":  client_public_ip,
                    "X-MACAddress":      "00:00:00:00:00:00",
                },
            )
            r.raise_for_status()
            body = r.json()
            if not body.get("status"):
                raise RuntimeError(f"AngelOne login failed: {body.get('message')}")
            data = body["data"]
            self.token       = data["jwtToken"]
            self.feed_token  = data.get("feedToken")
            self.token_expiry = datetime.utcnow() + timedelta(hours=20)
            logger.info(
                "position-stocks: AngelOne session refreshed (feed_token=%s...)",
                (self.feed_token or "")[:8],
            )

    def rest_headers(self) -> dict:
        return {
            "Authorization":    f"Bearer {self.token}",
            "X-PrivateKey":     self.api_key,
            "Content-Type":     "application/json",
            "Accept":           "application/json",
            "X-UserType":       "USER",
            "X-SourceID":       "WEB",
            "X-ClientLocalIP":  "127.0.0.1",
            "X-ClientPublicIP": _resolve_client_public_ip(),
            "X-MACAddress":     "00:00:00:00:00:00",
        }


# Module-level singleton — one session per process
_session = AngelOneSession()


def get_session() -> AngelOneSession:
    return _session
