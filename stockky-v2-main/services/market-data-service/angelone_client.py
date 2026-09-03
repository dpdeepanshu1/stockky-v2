"""
services/market-data-service/angelone_client.py  §1 of the master prompt.

AngelOne SmartAPI session manager with TOTP auto-refresh.
Primary quote/candle source, replacing yfinance as the first-choice provider.
"""
from __future__ import annotations
import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import httpx
import pyotp

try:
    from rate_limiter import acquire as _rl_acquire, in_cooldown as _rl_in_cooldown, set_cooldown as _rl_set_cooldown
except Exception:  # pragma: no cover — keep working even if rate_limiter.py is ever absent
    def _rl_acquire(provider, weight=1.0, max_wait=20.0):
        return 0.0
    def _rl_in_cooldown(provider):
        return False
    def _rl_set_cooldown(provider, seconds):
        pass

logger = logging.getLogger("angelone-client")

_BASE = "https://apiconnect.angelone.in"

# 2026-09-01 fix: this client made every REST call with zero rate limiting —
# AngelOne is now the primary quote/candle source (this module's own
# docstring above), so it takes the same shared-bucket treatment yfinance
# already had (see rate_limiter.py's "angelone_*" buckets and their
# reasoning). AngelOne's real 403 body on a rate-limit hit is
# {"status": false, "message": "Access denied because of exceeding access
# rate", "errorcode": "..."} per multiple SmartAPI Forum reports (topics
# 5560, 5636/5637) — detect it and cool down rather than let the caller's
# normal retry logic hammer it again immediately.
_ANGELONE_COOLDOWN_SEC = float(os.environ.get("ANGELONE_COOLDOWN_SEC", "30"))


def _is_rate_limit_response(status_code: int, body: Optional[dict]) -> bool:
    if status_code == 403:
        msg = ((body or {}).get("message") or "").lower()
        if "exceeding access rate" in msg or "access denied" in msg:
            return True
    return status_code == 429


def _safe_json(r: httpx.Response) -> Optional[dict]:
    try:
        return r.json()
    except Exception:
        return None


class AngelOneSession:
    """TOTP-authenticated AngelOne SmartAPI session. Thread-safe for async use."""

    def __init__(self) -> None:
        self.client_id   = os.environ.get("ANGELONE_CLIENT_ID", "")
        self.mpin        = os.environ.get("ANGELONE_MPIN", "")
        self.api_key     = os.environ.get("ANGELONE_API_KEY", "")
        self.totp_secret = os.environ.get("ANGELONE_TOTP_SECRET", "")
        self.token:        Optional[str]      = None
        self.feed_token:   Optional[str]      = None
        self.token_expiry: Optional[datetime] = None
        self._lock = asyncio.Lock()

    def is_configured(self) -> bool:
        return bool(self.client_id and self.mpin and self.api_key and self.totp_secret)

    async def ensure_session(self) -> None:
        """Refresh session if expired or missing. Thread-safe via lock."""
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
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{_BASE}/rest/auth/angelbroking/user/v1/loginByPassword",
                json={
                    "clientcode": self.client_id,
                    "password":   self.mpin,
                    "totp":       otp,
                },
                headers={
                    "X-PrivateKey":  self.api_key,
                    "Content-Type":  "application/json",
                    "Accept":        "application/json",
                    "X-UserType":    "USER",
                    "X-SourceID":    "WEB",
                    "X-ClientLocalIP": "127.0.0.1",
                    "X-ClientPublicIP": "127.0.0.1",
                    "X-MACAddress":  "00:00:00:00:00:00",
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
            logger.info("AngelOne session refreshed (expires in 20h)")

    def _headers(self) -> dict:
        # BUG FIX: this was missing X-UserType/X-SourceID/X-ClientLocalIP/
        # X-ClientPublicIP/X-MACAddress — AngelOne's secure market-data
        # endpoints (quote/candles), not just login, require the full
        # header set (confirmed empirically: our manual curl test only
        # succeeded once all of these were present). Without them, every
        # get_quote/get_quotes_batch/get_candles call after a successful
        # login was silently failing server-side, angelone_ws_feed.py's
        # poll loop caught the exception and logged a warning per batch,
        # live_quotes/_LIVE never actually got populated, and the whole
        # system kept falling through to Yahoo/yfinance regardless of
        # AngelOne being configured and logging in fine. This is the
        # concrete root cause of "AngelOne configured but not giving
        # real data."
        return {
            "Authorization":     f"Bearer {self.token}",
            "X-PrivateKey":      self.api_key,
            "Content-Type":      "application/json",
            "Accept":            "application/json",
            "X-UserType":        "USER",
            "X-SourceID":        "WEB",
            "X-ClientLocalIP":   "127.0.0.1",
            "X-ClientPublicIP":  os.environ.get("ANGELONE_STATIC_IP", "127.0.0.1"),
            "X-MACAddress":      "00:00:00:00:00:00",
        }

    async def get_quote(self, exchange: str, symbol_token: str) -> dict:
        """Fetch live quote for one symbol token."""
        if _rl_in_cooldown("angelone_quote"):
            return {}
        await self.ensure_session()
        _rl_acquire("angelone_quote", weight=1)
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{_BASE}/rest/secure/angelbroking/market/v1/quote/",
                headers=self._headers(),
                json={
                    "mode": "FULL",
                    "exchangeTokens": {exchange: [symbol_token]},
                },
            )
            if _is_rate_limit_response(r.status_code, _safe_json(r)):
                _rl_set_cooldown("angelone_quote", _ANGELONE_COOLDOWN_SEC)
                return {}
            r.raise_for_status()
            body = r.json()
            fetched = (body.get("data") or {}).get("fetched") or []
            if fetched:
                return fetched[0]
            return {}

    async def get_candles(
        self,
        exchange: str,
        symbol_token: str,
        interval: str,
        from_date: str,
        to_date: str,
    ) -> list:
        """Fetch OHLCV candles. interval: ONE_MINUTE/FIVE_MINUTE/ONE_DAY etc."""
        if _rl_in_cooldown("angelone_candle"):
            return []
        await self.ensure_session()
        _rl_acquire("angelone_candle", weight=1)
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{_BASE}/rest/secure/angelbroking/historical/v1/getCandleData",
                headers=self._headers(),
                json={
                    "exchange":    exchange,
                    "symboltoken": symbol_token,
                    "interval":    interval,
                    "fromdate":    from_date,
                    "todate":      to_date,
                },
            )
            if _is_rate_limit_response(r.status_code, _safe_json(r)):
                _rl_set_cooldown("angelone_candle", _ANGELONE_COOLDOWN_SEC)
                return []
            r.raise_for_status()
            body = r.json()
            return body.get("data") or []

    async def get_quotes_batch(self, exchange: str, symbol_tokens: list) -> list:
        """Fetch live quotes for multiple tokens in one call. AngelOne's
        quote endpoint documents a cap of 50 tokens per exchange per
        request — callers must chunk larger lists themselves (see
        angelone_ws_feed.py's polling loop, which chunks in batches of 50)."""
        if not symbol_tokens:
            return []
        if _rl_in_cooldown("angelone_quote"):
            return []
        await self.ensure_session()
        # One HTTP call regardless of how many tokens are in this batch (up
        # to the 50-token cap), so this costs the same ONE unit against the
        # angelone_quote bucket as a single-symbol get_quote() call — same
        # reasoning as yfinance's batch weight capping (see rate_limiter.py).
        _rl_acquire("angelone_quote", weight=1)
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{_BASE}/rest/secure/angelbroking/market/v1/quote/",
                headers=self._headers(),
                json={"mode": "FULL", "exchangeTokens": {exchange: symbol_tokens}},
            )
            if _is_rate_limit_response(r.status_code, _safe_json(r)):
                _rl_set_cooldown("angelone_quote", _ANGELONE_COOLDOWN_SEC)
                return []
            r.raise_for_status()
            body = r.json()
            return (body.get("data") or {}).get("fetched") or []



# Module-level singleton — shared across the service process
_session = AngelOneSession()


def get_session() -> AngelOneSession:
    return _session
