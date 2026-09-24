"""
notifier.py — direct Telegram notifications for this service's own
CRITICAL events (broker order-type/price mismatches, dead EOD flat-SELLs,
unresolvable legacy exit-order backfills).

Session41 fix (STATUS.md open item #7): before this file existed, every
`logger.critical(...)` call in execution/dhan_client.py and
orders/reconcile.py was log-only — nothing paged a human when this
service's own self-checks caught a broker-side mismatch or a dead exit
order. This module is a straight port of real-trade-service/notifier.py
(duplicated on purpose, same isolation rationale as every other module in
this service — see config.py's module docstring), kept deliberately
identical in behaviour so both services' CRITICAL alerts land on the same
Telegram chat via the same config.

Routing: POST http://notification-scheduler-service:8000/notification/notify
  {"title": "...", "message": "...", "channel": "telegram"}

Fallback: if the notification service is unreachable, falls back to
calling Telegram directly using TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
env vars.

Deliberately best-effort: a notification failure must NEVER block or
fail an order path. Every function swallows its own exceptions.

session42 audit: added message deduplication.
Without it, a rejection storm (DATAMATICS: 60+ SELL rejections) fired
60+ identical Telegram messages — one per rejection, all the same text.
The dedup cache keeps a hash of the last N unique messages and their
send-time. Any message identical to one sent within DEDUP_WINDOW_S is
dropped silently (the original alert already reached the operator).
Different messages (different symbol, different error text) always send.

session112 fix (port of real-trade-service's session111 fix — see
archive/session-notes/SESSION111_NOTIFY_FIRE_AND_FORGET_EXIT_PATH_FIX_2026-09-25.md):
notify_sync is fully synchronous end to end — httpx.post to
notification-scheduler-service (12s timeout), on failure a direct
Telegram call (15s timeout), on a non-200/HTML-parse failure a second
direct Telegram attempt as plain text (another 15s timeout). Worst case
~42s. Every order-path caller in this service (orders/entry.py,
orders/breakeven.py, orders/eod_squareoff.py, orders/overnight_stop.py,
orders/reconcile.py) ran this inline inside a to_thread-wrapped stage of
either _run_cycle() (under _cycle_lock, shared with screening/entry for
every OTHER candidate that cycle) or _fast_reconcile_loop() (no lock, but
a single sequential while-loop, so a slow call here delays that loop's
own next 5-10s tick the same way) — see main.py. notify_critical()'s two
callers in main.py's eDIS morning check are worse still: called directly
on the event loop with no to_thread wrapper at all, so a blocking
notify_sync there could stall every request this service was handling.
notify_fire_and_forget() (below) fixes this without touching the shape of
any call site's message text — every one of them was already a
fire-and-forget statement with no caller using the return value.
"""
from __future__ import annotations

import hashlib
import logging
import re as _re
import threading
import time
from collections import OrderedDict

import httpx

import config

logger = logging.getLogger("position-stocks-notifier")

# ── Bot-token hygiene (session98) ─────────────────────────────────────────────
# Telegram's bot token is part of the URL (https://api.telegram.org/bot<TOKEN>/
# sendMessage) and httpx logs every request at INFO as
#   HTTP Request: POST https://api.telegram.org/bot<TOKEN>/sendMessage "HTTP/1.1 200 OK"
# while this service runs logging.basicConfig(level=logging.INFO) — so the token
# was written to the log on every send. This filter rewrites it out of the
# record before any handler sees it. (Same fix as real-trade-service/notifier.py.)
class _TelegramTokenRedactingFilter(logging.Filter):
    _TOKEN_IN_URL = _re.compile(r"/bot\d+:[\w-]+/")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            redacted = self._TOKEN_IN_URL.sub("/bot***/", msg)
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

_NOTIFICATION_SERVICE_URL = f"{config.NOTIFICATION_SERVICE_URL}"

# ── Message deduplication ─────────────────────────────────────────────────────
# Keeps the last _DEDUP_CACHE_SIZE message hashes and when they were sent.
# Any message with the same hash within DEDUP_WINDOW_S is suppressed.
_DEDUP_WINDOW_S    = 300   # 5 minutes — one alert per unique message per 5 min
_DEDUP_CACHE_SIZE  = 64    # max unique messages to track at once
_dedup_cache: OrderedDict[str, float] = OrderedDict()   # hash → sent_at monotonic


def _should_send(text: str) -> bool:
    """Return True if this message should be sent (not a recent duplicate)."""
    h = hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()  # noqa: S324 — non-crypto
    now = time.monotonic()
    last = _dedup_cache.get(h)
    if last is not None and (now - last) < _DEDUP_WINDOW_S:
        return False   # duplicate within window — suppress
    # Record / refresh
    if h in _dedup_cache:
        _dedup_cache.move_to_end(h)
    _dedup_cache[h] = now
    while len(_dedup_cache) > _DEDUP_CACHE_SIZE:
        _dedup_cache.popitem(last=False)
    return True


def notify_sync(text: str) -> bool:
    """Synchronous, best-effort. Tries notification-scheduler-service
    first (uses the Alert panel's saved Telegram config), falls back to
    a direct Telegram call using env vars. Never raises.

    Deduplicates: identical messages within DEDUP_WINDOW_S (5 min) are
    dropped silently — the original alert already reached the operator.

    BLOCKS the calling thread for the full delivery attempt — worst case
    the 12s service timeout PLUS the 15s direct-Telegram timeout PLUS a
    second 15s HTML-retry-as-plain-text attempt inside _direct_telegram,
    ~42s. Prefer notify_fire_and_forget() (or notify_critical(), which now
    uses it) from order/execution code — see notify_fire_and_forget's
    docstring for why. notify_sync remains the right choice only for a
    caller with nothing else waiting on it."""
    if not _should_send(text):
        logger.debug("notifier: duplicate message suppressed within dedup window")
        return True   # treat as "delivered" — operator was already notified
    return _deliver_sync(text)


def notify_fire_and_forget(text: str) -> None:
    """Non-blocking variant of notify_sync, for call sites that cannot
    afford to block on network I/O while holding a shared resource.

    session112 fix (ported from real-trade-service's session111 fix):
    every order-path caller of notify_sync in this service — entry.py's
    BUY-placed alerts, breakeven.py's stop-moved alert,
    eod_squareoff.py's EOD-sent/capped-out alerts, overnight_stop.py's
    overnight STOP_HIT/partial-fill alerts, reconcile.py's fill-resolved/
    target-hit/stop-hit alerts — runs inside a to_thread-wrapped stage of
    either _run_cycle() (under _cycle_lock, shared with screening/entry for
    every OTHER candidate that cycle) or _fast_reconcile_loop() (no lock,
    but a single sequential while-loop, so a slow call here delays that
    loop's own next 5-10s tick the same way). Two of main.py's own
    notify_critical() calls (the eDIS morning check) are worse still —
    called directly on the event loop with no to_thread wrapper at all, so
    a blocking notify_sync there stalls EVERY request this service is
    handling, not just its own background loop. notify_critical() is
    redefined below to use this function instead, which fixes those two
    call sites with no code change at their end.

    The dedup check (_should_send) stays on the calling thread — cheap,
    in-memory, and keeping it here (not in the background thread) keeps
    the "identical message within 5 minutes is suppressed" guarantee
    exact, with no race between two near-simultaneous fire-and-forget
    calls for the same text. Only the actual network I/O moves to a
    daemon thread. No return value: by the time delivery finishes, the
    caller has moved on and there is no one left to hand a result to —
    the same "never block or fail an order path" contract this module's
    docstring states, taken to its conclusion for a caller that can't
    wait at all."""
    if not _should_send(text):
        logger.debug("notifier: duplicate message suppressed within dedup window")
        return
    try:
        threading.Thread(target=_deliver_background, args=(text,), daemon=True, name="notify-bg").start()
    except Exception:
        # Starting the thread itself failed (e.g. resource limits) — this
        # is still just a notification, never let it surface to the caller.
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
    try:
        resp = httpx.post(
            f"{_NOTIFICATION_SERVICE_URL}/notify",
            json={"title": "Stockky Position Scalp", "message": text, "channel": "telegram"},
            timeout=12.0,
        )
        if resp.status_code == 200:
            result = resp.json()
            if result.get("delivered"):
                return True
            logger.debug("Notification service returned not-delivered: %s", result.get("note"))
    except Exception as e:
        logger.debug("Notification service unreachable (%s) — trying direct Telegram fallback", e)

    return _direct_telegram(text)


def _direct_telegram(text: str) -> bool:
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        logger.debug("Direct Telegram fallback: env vars not set — notification dropped.")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Same HTML-over-Markdown fix as real-trade-service's notifier.py
    # (2026-09-07): Markdown silently drops messages containing
    # unescaped `_`, `.`, `(` etc, common in symbol names / Dhan error text.
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
            try:
                plain = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
                resp2 = httpx.post(url, json=plain, timeout=15.0)
                return resp2.status_code == 200
            except Exception:
                return False
        return True
    except Exception as e:
        logger.warning("Direct Telegram notify error: %s", _TelegramTokenRedactingFilter._TOKEN_IN_URL.sub("/bot***/", str(e)))
        return False


def notify_critical(text: str) -> None:
    """Fire-and-forget wrapper for CRITICAL-log call sites — never raises,
    never blocks an order path on notification latency/failure. Prefer
    this over calling notify_sync directly from execution/order code.

    session112 fix: this was ALREADY documented as fire-and-forget and
    non-blocking, but its implementation called the blocking notify_sync
    directly — a try/except around a synchronous call is not fire-and-
    forget, it just makes a blocking call that also can't raise. Two of
    this function's own callers (main.py's eDIS morning check) run
    directly on the event loop with no to_thread wrapper at all, so this
    was the single worst call-site instance of the notify_sync blocking
    bug: every request this service was handling could stall for up to
    ~42s. Now genuinely non-blocking, via notify_fire_and_forget."""
    try:
        notify_fire_and_forget(f"\U0001F6A8 <b>CRITICAL</b>\n{text}")
    except Exception as e:  # noqa: BLE001 — notification must never break the caller
        logger.debug("notify_critical: swallowed notification error: %s", e)
