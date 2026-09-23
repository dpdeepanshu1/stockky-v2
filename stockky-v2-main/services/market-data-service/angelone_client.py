"""
services/market-data-service/angelone_client.py  §1 of the master prompt.

AngelOne SmartAPI session manager with TOTP auto-refresh.
Primary quote/candle source, replacing yfinance as the first-choice provider.
"""
from __future__ import annotations
import asyncio
import logging
import os
import threading as _threading
import time
import weakref as _weakref
from datetime import datetime, timedelta
from typing import Optional

import httpx
import pyotp

try:
    from rate_limiter import acquire as _rl_acquire, in_cooldown as _rl_in_cooldown, set_cooldown as _rl_set_cooldown
    from rate_limiter import try_acquire as _rl_try_acquire
except Exception:  # pragma: no cover — keep working even if rate_limiter.py is ever absent
    def _rl_acquire(provider, weight=1.0, max_wait=20.0):
        return 0.0
    def _rl_try_acquire(provider, weight=1.0, max_wait=5.0):
        return True
    def _rl_in_cooldown(provider):
        return False
    def _rl_set_cooldown(provider, seconds):
        pass

logger = logging.getLogger("angelone-client")

_BASE = "https://apiconnect.angelone.in"

# 2026-09-07 fix — ROOT CAUSE of the recurring 403 on the secure quote/
# candle endpoints (session21 investigation): X-ClientPublicIP was being
# sent as the LITERAL STRING "127.0.0.1" on every single call — both the
# hardcoded value in _login() below, and the os.environ.get("ANGELONE_STATIC_IP",
# "127.0.0.1") fallback in _headers(). Confirmed via `docker compose exec
# market-data-service printenv | grep ANGELONE`: ANGELONE_STATIC_IP is not
# set anywhere in this deployment (grep for it across the whole repo turns
# up zero hits outside this file — it was never wired into docker-compose,
# .env.example, or documented anywhere), so every request was going out
# with a loopback address as its "client public IP". AngelOne's SmartAPI
# validates this header (it's part of the same security header set that
# _headers()'s own docstring already identified as required) — a
# non-routable loopback address is never going to match anything on an
# IP-whitelist and is a very plausible, concrete explanation for a 403 that
# a rate-limit or a stale-token theory couldn't otherwise account for.
#
# Fix: resolve a REAL IP once (prefer an explicit ANGELONE_STATIC_IP if the
# operator has one — e.g. behind a static-IP proxy — otherwise auto-detect
# this container's actual outbound public IP via a public IP-echo service,
# cached so we're not hitting that service on every request) and use it
# everywhere X-ClientPublicIP is sent. 127.0.0.1 is kept ONLY as an
# absolute last-resort if every detection method fails, and that case now
# logs a loud, explicit warning instead of silently sending a value that
# can never work.
_outbound_ip_cache: dict[str, float | str | None] = {"ip": None, "at": 0.0}
_OUTBOUND_IP_TTL_SECONDS = 15 * 60  # redeploys can change the egress IP


def get_outbound_ip() -> Optional[str]:
    """Best-effort real public IP this container is actually egressing
    from right now. Same idea as real-trade-service's
    execution.dhan_client.get_outbound_ip() — kept separate here since
    these are independent microservices with no shared import path."""
    try:
        import httpx as _httpx  # local import: keep this cheap/optional
        resp = _httpx.get("https://api.ipify.org?format=json", timeout=6.0)
        resp.raise_for_status()
        return resp.json().get("ip")
    except Exception as e:
        logger.warning("angelone get_outbound_ip: lookup failed: %s", e)
        return None


def _resolve_client_public_ip() -> str:
    """What to put in X-ClientPublicIP. Priority: explicit ANGELONE_STATIC_IP
    env var (operator knows better, e.g. a whitelisted static-IP proxy) ->
    cached auto-detected outbound IP -> loud-warning 127.0.0.1 fallback."""
    explicit = os.environ.get("ANGELONE_STATIC_IP", "").strip()
    if explicit:
        return explicit

    now = time.time()
    cached_ip = _outbound_ip_cache["ip"]
    cached_at = _outbound_ip_cache["at"]
    if cached_ip and (now - cached_at) < _OUTBOUND_IP_TTL_SECONDS:
        return cached_ip

    detected = get_outbound_ip()
    if detected:
        _outbound_ip_cache["ip"] = detected
        _outbound_ip_cache["at"] = now
        return detected

    logger.warning(
        "angelone: could not resolve a real outbound IP (no ANGELONE_STATIC_IP "
        "set and auto-detection failed) — falling back to 127.0.0.1, which "
        "AngelOne WILL reject with a 403 on secure endpoints. Set "
        "ANGELONE_STATIC_IP or check outbound network access to api.ipify.org."
    )
    return "127.0.0.1"

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


# 2026-09-21: the historical/quote endpoints were returning waves of 403s but
# httpx's INFO line only shows the status code, never AngelOne's body — so
# "rate limit" vs "IP not whitelisted / bad session" (completely different
# fixes) could not be told apart from the logs. Log the body of any 403/429,
# throttled per endpoint so a storm doesn't itself flood the log.
_DENIED_LOG_EVERY_S = 60.0
_denied_last_logged: dict[str, float] = {}


def _log_denied(endpoint: str, r: httpx.Response) -> None:
    if r.status_code not in (403, 429):
        return
    now = time.time()
    if now - _denied_last_logged.get(endpoint, 0.0) < _DENIED_LOG_EVERY_S:
        return
    _denied_last_logged[endpoint] = now
    logger.warning(
        "AngelOne %s returned HTTP %d — body: %s (logged at most once per %.0fs per endpoint)",
        endpoint, r.status_code, (r.text or "")[:300].replace("\n", " "), _DENIED_LOG_EVERY_S,
    )


# Candle calls that can't get a rate-limit token within this many seconds are
# SKIPPED (caller falls back to yfinance) instead of being let through anyway —
# see rate_limiter.try_acquire.
_CANDLE_MAX_WAIT_S = float(os.environ.get("ANGELONE_CANDLE_MAX_WAIT_S", "15"))


class AngelOneSession:
    """TOTP-authenticated AngelOne SmartAPI session. Thread-safe for async use.

    2026-09-21 fix ("is bound to a different event loop" crash, seen in
    market-data-service logs as `ERROR:angelone-ws-feed:AngelOne feed
    error: <asyncio.locks.Lock object ...> is bound to a different event
    loop`): this session is a module-level singleton (see `_session`
    below) that's called from TWO different event loops in the same
    process — the main uvicorn loop (every FastAPI request handler that
    calls ensure_session()/get_quote()/etc, e.g. main.py) AND
    angelone_ws_feed.py's dedicated background thread, which spins up its
    own `asyncio.new_event_loop()` and calls `session.ensure_session()`
    on it. A single `asyncio.Lock()` instance lazily binds to whichever
    loop first calls `.acquire()` on it (Python 3.10+ behavior) — every
    subsequent `await` from the OTHER loop then raises exactly that
    RuntimeError. This crashed the ws-feed thread's poll loop every time
    a token refresh happened to be needed while a request was also being
    served on the main loop (or vice versa).

    2026-09-21 fix, part 2 (memory leak from part 1): `self._locks` was a
    plain dict keyed by event-loop object. main.py has several sync route
    handlers (`/angelone/movers`, per-request quote/candle lookups) that
    call `asyncio.run(...)` — each `asyncio.run()` call creates a BRAND
    NEW, one-shot event loop that's discarded the instant the call
    returns. A plain dict holds a strong reference to its keys, so every
    one of those throwaway loops (and everything reachable from it) was
    being pinned in memory forever, one new dict entry per request,
    forever — an unbounded leak. Switched to `weakref.WeakKeyDictionary`:
    once nothing else references a given loop (i.e. right after
    `asyncio.run()` returns and closes it), its entry — and that lock —
    is dropped automatically. The long-lived loops (the main uvicorn loop,
    the ws-feed thread's loop) behave exactly as before, since they stay
    referenced for the life of the process.
    """

    def __init__(self) -> None:
        self.client_id   = os.environ.get("ANGELONE_CLIENT_ID", "")
        self.mpin        = os.environ.get("ANGELONE_MPIN", "")
        self.api_key     = os.environ.get("ANGELONE_API_KEY", "")
        self.totp_secret = os.environ.get("ANGELONE_TOTP_SECRET", "")
        self.token:        Optional[str]      = None
        self.feed_token:   Optional[str]      = None
        self.token_expiry: Optional[datetime] = None
        self._locks: "_weakref.WeakKeyDictionary" = _weakref.WeakKeyDictionary()  # event loop -> asyncio.Lock
        self._locks_guard = _threading.Lock()         # guards self._locks only

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        with self._locks_guard:
            lock = self._locks.get(loop)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[loop] = lock
        return lock

    def is_configured(self) -> bool:
        return bool(self.client_id and self.mpin and self.api_key and self.totp_secret)

    async def ensure_session(self) -> None:
        """Refresh session if expired or missing. Thread-safe via lock."""
        async with self._get_lock():
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
        # 2026-09-07 fix: was hardcoded "127.0.0.1" — see module comment
        # above _resolve_client_public_ip(). Login is a secure endpoint
        # too, so it needs the same real-IP fix as _headers() below.
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
                    "X-PrivateKey":  self.api_key,
                    "Content-Type":  "application/json",
                    "Accept":        "application/json",
                    "X-UserType":    "USER",
                    "X-SourceID":    "WEB",
                    "X-ClientLocalIP": "127.0.0.1",
                    "X-ClientPublicIP": client_public_ip,
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
            # 2026-09-07 fix: was a raw os.environ.get(..., "127.0.0.1")
            # default with nothing ever setting ANGELONE_STATIC_IP in this
            # deployment — every call silently sent the loopback address.
            # See module comment above _resolve_client_public_ip().
            "X-ClientPublicIP":  _resolve_client_public_ip(),
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
            _log_denied("quote", r)
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
        # Fail-CLOSED limiter (2026-09-21): get_candles is reached from 100+
        # concurrent /history worker threads (asyncio.run per thread, so this
        # blocking wait only ever parks that one thread). The old fail-open
        # acquire() let EVERY waiter through at max_wait, so a burst became a
        # synchronized spike far past AngelOne's ~3 req/s getCandleData
        # ceiling → the 403 "exceeding access rate" wall. Now the excess is
        # shed (returns [] → /history falls back to yfinance, same as when a
        # cooldown is active) and only token-holders hit AngelOne.
        if not _rl_try_acquire("angelone_candle", weight=1, max_wait=_CANDLE_MAX_WAIT_S):
            return []
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
            _log_denied("getCandleData", r)
            if _is_rate_limit_response(r.status_code, _safe_json(r)):
                _rl_set_cooldown("angelone_candle", _ANGELONE_COOLDOWN_SEC)
                return []
            r.raise_for_status()
            body = r.json()
            return body.get("data") or []

    async def get_gainers_losers(self, datatype: str = "PercPriceGainers", expirytype: str = "NEAR") -> list:
        """
        !! F&O DERIVATIVES SEGMENT ONLY — NOT a cash-equity screener !!

        2026-09-04 CORRECTION: this was originally added (and briefly wired
        into /angelone/movers) on the wrong assumption that it was a
        whole-market equity gainers/losers board. It is not. AngelOne's own
        SmartAPI announcement is explicit: "Top Gainers/Losers API gives
        you the Top gainers and Losers in the DERIVATIVES SEGMENT for the
        day" — rows come back as futures contracts (e.g.
        "HDFCBANK25JAN24FUT"), scoped to the ~200 F&O-eligible large/mid
        caps only. Two consequences that made it the wrong tool for
        _get_momentum_movers(): (1) accounts without the F&O segment
        activated get a flat 403 on this endpoint — that's an access
        restriction, not a rate-limit or bug; (2) even with F&O access, it
        structurally cannot surface small/midcap volume-shockers (the
        actual gap this codebase needs filled — see session19g's Groww
        screenshot names, almost none of which are F&O stocks).
        /angelone/movers now does a real whole-market LTP sweep instead
        (see market-data-service main.py) and does NOT call this method.
        Left in place only in case a future F&O-specific feature
        (PCR/OI-buildup signals) wants it — datatype:
        PercPriceGainers | PercPriceLosers | PercOIGainers | PercOILosers.
        """
        if _rl_in_cooldown("angelone_gainers"):
            return []
        await self.ensure_session()
        _rl_acquire("angelone_gainers", weight=1)
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{_BASE}/rest/secure/angelbroking/marketData/v1/gainersLosers",
                headers=self._headers(),
                json={"datatype": datatype, "expirytype": expirytype},
            )
            if _is_rate_limit_response(r.status_code, _safe_json(r)):
                _rl_set_cooldown("angelone_gainers", _ANGELONE_COOLDOWN_SEC)
                return []
            r.raise_for_status()
            body = r.json()
            if not body.get("status"):
                logger.warning("AngelOne gainersLosers(%s): %s", datatype, body.get("message"))
                return []
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
            _log_denied("quote(batch)", r)
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
