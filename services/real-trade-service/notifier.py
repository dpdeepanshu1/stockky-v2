"""
notifier.py — direct Telegram notifications for this service's own
events (BUY sent, broker fill confirmed, SELL sent, auto-pilot cycle
summary). Separate from notification-scheduler-service's scan/candidate
notifications — but now ROUTES THROUGH the same notification-scheduler-
service so both use the single Telegram config saved on the Alert panel.

Routing: POST http://notification-scheduler-service:8000/notification/notify
  {"title": "...", "message": "...", "channel": "telegram"}

Fallback: if the notification service is unreachable, falls back to
calling Telegram directly using TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
env vars (original behaviour) — so .env config still works as backup.

Deliberately best-effort: a notification failure must NEVER block or
fail an order path. Every function swallows its own exceptions.

notify_sync() itself can still block the CALLING thread for its full
delivery attempt (worst case ~42s — see its docstring). For a call site
that cannot afford that (anything running inside a shared lock or a
tight loop), use notify_fire_and_forget() instead, which does the exact
same dedup + delivery but off-thread and with no return value.
"""
from __future__ import annotations
import asyncio

import hashlib
import logging
import os
import re
import threading
import time
from collections import OrderedDict

import httpx

import config

logger = logging.getLogger("real-trade-notifier")


# ── Bot-token hygiene (session98) ─────────────────────────────────────────────
# _direct_telegram() calls https://api.telegram.org/bot<TOKEN>/sendMessage — the
# bot token is part of the URL. httpx logs every request at INFO as
#   HTTP Request: POST https://api.telegram.org/bot<TOKEN>/sendMessage "HTTP/1.1 200 OK"
# and main.py runs logging.basicConfig(level=logging.INFO) with nothing muting
# the "httpx" logger, so on every fallback send the token went into the service
# log (and anything that ships it). Fix: a filter on the "httpx" logger that
# rewrites the token out of the record before any handler sees it, plus the same
# scrubbing on the error text this module logs itself.
_TOKEN_IN_URL_RE = re.compile(r"/bot\d+:[\w-]+/")
_MIN_LITERAL_TOKEN_LEN = 8


def _redact_token(text) -> str:
    """str(text) with any '/bot<id>:<secret>/' URL segment -> '/bot***/' and the
    configured TELEGRAM_BOT_TOKEN (if long enough to be real) -> '***'.
    Never raises."""
    try:
        out = _TOKEN_IN_URL_RE.sub("/bot***/", str(text))
        token = getattr(config, "TELEGRAM_BOT_TOKEN", "") or ""
        if len(token) >= _MIN_LITERAL_TOKEN_LEN:
            out = out.replace(token, "***")
        return out
    except Exception:
        return "<text withheld: redaction failed>"


class _TelegramTokenRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            redacted = _redact_token(msg)
            if redacted != msg:
                record.msg, record.args = redacted, ()
        except Exception:
            pass   # a logging filter must never break logging
        return True


def _install_httpx_token_filter() -> None:
    """Idempotent — safe on module reload."""
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(f, _TelegramTokenRedactingFilter) for f in httpx_logger.filters):
        httpx_logger.addFilter(_TelegramTokenRedactingFilter())


_install_httpx_token_filter()

# Internal URL to notification-scheduler-service (docker-compose service name)
# Injected via NOTIFICATION_SERVICE_URL env var (added to docker-compose.yml)
# Falls back to the standard compose hostname if not set.
_NOTIFICATION_SERVICE_URL = os.getenv(
    "NOTIFICATION_SERVICE_URL",
    "http://notification-scheduler-service:8000/notification",
).rstrip("/")

# ── Message deduplication (session42 audit) ───────────────────────────────────
# Prevents alert storms: identical messages within _DEDUP_WINDOW_S are dropped.
_DEDUP_WINDOW_S   = 300
_DEDUP_CACHE_SIZE = 64
_dedup_cache: OrderedDict[str, float] = OrderedDict()


def _should_send(text: str) -> bool:
    h = hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()  # noqa: S324
    now = time.monotonic()
    last = _dedup_cache.get(h)
    if last is not None and (now - last) < _DEDUP_WINDOW_S:
        return False
    if h in _dedup_cache:
        _dedup_cache.move_to_end(h)
    _dedup_cache[h] = now
    while len(_dedup_cache) > _DEDUP_CACHE_SIZE:
        _dedup_cache.popitem(last=False)
    return True


def is_configured() -> bool:
    """Always True when notification-scheduler-service is reachable
    (it has its own Telegram config). Also True if env vars are set."""
    return True  # best-effort — we always try; silence is logged, not raised


async def notify_async(text: str) -> bool:
    """Fire-and-forget notification. Tries notification-scheduler-service
    first (uses Alert panel Telegram config), falls back to direct Telegram
    env-var call. Deduplicates identical messages within 5 minutes.
    Returns False (never raises) on failure."""
    if not _should_send(text):
        logger.debug("notifier: duplicate message suppressed within dedup window")
        return True
    # Primary: route through notification service
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(
                f"{_NOTIFICATION_SERVICE_URL}/notify",
                json={"title": "Stockky Trade", "message": text, "channel": "telegram"},
            )
        if resp.status_code == 200:
            result = resp.json()
            if result.get("delivered"):
                return True
            logger.debug("Notification service returned not-delivered: %s", result.get("note"))
            # Fall through to direct fallback below
    except Exception as e:
        logger.debug("Notification service unreachable (%s) — trying direct Telegram fallback", e)

    # Fallback: direct Telegram using env vars
    return (await asyncio.to_thread(_direct_telegram, text))


def notify_sync(text: str) -> bool:
    """Synchronous variant for call sites that aren't in an async function.
    Same routing logic as notify_async. Deduplicates identical messages
    within 5 minutes.

    BLOCKS the calling thread for the full delivery attempt — worst case
    the 12s service timeout PLUS the 15s direct-Telegram timeout PLUS a
    second 15s HTML-retry-as-plain-text attempt inside _direct_telegram,
    ~42s. Fine for one-off callers (adaptive_thresholds.py's startup
    notice, dhan_credentials.py's TOTP alerts) that have nothing else
    waiting on them. NOT fine for a call site sitting inside a shared lock
    or a tight polling loop — use notify_fire_and_forget() there instead
    (see its docstring for why exit_engine specifically needs it)."""
    if not _should_send(text):
        logger.debug("notifier: duplicate message suppressed within dedup window")
        return True
    return _deliver_sync(text)


def notify_fire_and_forget(text: str) -> None:
    """Non-blocking variant of notify_sync, for call sites that cannot
    afford to block on network I/O while holding a shared resource.

    session111 fix: exit_engine/exit.py's per-position exit loop runs
    inside _run_exit_tick_sync's per-mode exit lock (see auto_pilot.py's
    _run_exit_tick_sync docstring) and calls notify_sync after every
    SELL-sent / blocked-exit / fill-confirmation event — up to 14 call
    sites per evaluate_mode() pass. Each of those calls could block the
    thread (and therefore the lock) for up to notify_sync's ~42s worst
    case. Since the exit tick is skip-if-busy and fires every 5-10s, one
    slow Telegram delivery for position A could delay protective
    stop-loss evaluation for every OTHER open position in the same tick,
    and skip the next 5-10s tick outright while still holding the lock.

    The dedup check (_should_send) stays on the calling thread — it's a
    cheap in-memory lookup, and doing it here (not in the background
    thread) keeps the "identical message within 5 minutes is suppressed"
    guarantee exact, with no race between two near-simultaneous fire-and-
    forget calls for the same text. Only the actual network I/O (the part
    that can take seconds) moves to a daemon thread. There is deliberately
    no return value: by the time delivery finishes, the exit loop that
    triggered it has moved on and there is no one left to hand a result
    to — this is the same "never block or fail an order path" contract
    notify_sync documents, taken to its logical conclusion for a caller
    that can't wait at all."""
    if not _should_send(text):
        logger.debug("notifier: duplicate message suppressed within dedup window")
        return
    try:
        threading.Thread(target=_deliver_background, args=(text,), daemon=True, name="notify-bg").start()
    except Exception:
        # Starting the thread itself failed (e.g. resource limits) — this is
        # still just a notification, never let it surface to the caller.
        logger.debug("notify_fire_and_forget: failed to start background delivery thread", exc_info=True)


def _deliver_background(text: str) -> None:
    """Runs _deliver_sync on the background thread notify_fire_and_forget
    starts. Swallows everything — _deliver_sync already catches its own
    network exceptions, this is just a last-resort guard so a bug in the
    delivery path can never crash the (silent, unjoined) thread loudly."""
    try:
        _deliver_sync(text)
    except Exception:
        logger.debug("notify_fire_and_forget: background delivery failed", exc_info=True)


def _deliver_sync(text: str) -> bool:
    """The actual synchronous delivery attempt (service, then direct
    Telegram fallback) — shared by notify_sync (blocking) and
    notify_fire_and_forget (via a background thread). Assumes the dedup
    check has already been done by the caller."""
    # Primary: notification service
    try:
        resp = httpx.post(
            f"{_NOTIFICATION_SERVICE_URL}/notify",
            json={"title": "Stockky Trade", "message": text, "channel": "telegram"},
            timeout=12.0,
        )
        if resp.status_code == 200:
            result = resp.json()
            if result.get("delivered"):
                return True
            logger.debug("Notification service returned not-delivered: %s", result.get("note"))
    except Exception as e:
        logger.debug("Notification service unreachable (%s) — trying direct Telegram fallback", e)

    # Fallback: direct Telegram using env vars
    return _direct_telegram(text)


def _direct_telegram(text: str) -> bool:
    """Direct Telegram call using TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
    env vars — original behaviour, kept as fallback."""
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        logger.debug("Direct Telegram fallback: env vars not set — notification dropped.")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # BUG FIX (2026-09-07): switched to HTML parse_mode — Markdown silently
    # drops messages containing unescaped `_`, `.`, `(` etc. (common in
    # trade messages with rupee amounts, symbol names and Dhan error text).
    # Convert *bold* markers from trade messages to <b>bold</b> HTML.
    import re as _re
    html_text = _re.sub(r'\*([^*]+)\*', r'<b>\1</b>', text)
    payload = {
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = httpx.post(url, json=payload, timeout=15.0)
        if resp.status_code != 200:
            logger.warning("Direct Telegram notify failed (%s): %s", resp.status_code, resp.text[:200])
            # Plain-text last resort — still gets the message through even if HTML fails
            try:
                plain = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
                resp2 = httpx.post(url, json=plain, timeout=15.0)
                return resp2.status_code == 200
            except Exception:
                return False
        return True
    except Exception as e:
        logger.warning("Direct Telegram notify error: %s", _redact_token(e))
        return False
