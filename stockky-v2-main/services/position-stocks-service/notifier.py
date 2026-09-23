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
"""
from __future__ import annotations

import hashlib
import logging
import re as _re
import time
from collections import OrderedDict

import httpx

import config

logger = logging.getLogger("position-stocks-notifier")

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
    dropped silently — the original alert already reached the operator."""
    if not _should_send(text):
        logger.debug("notifier: duplicate message suppressed within dedup window")
        return True   # treat as "delivered" — operator was already notified
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
        logger.warning("Direct Telegram notify error: %s", e)
        return False


def notify_critical(text: str) -> None:
    """Fire-and-forget wrapper for CRITICAL-log call sites — never raises,
    never blocks an order path on notification latency/failure. Prefer
    this over calling notify_sync directly from execution/order code."""
    try:
        notify_sync(f"\U0001F6A8 <b>CRITICAL</b>\n{text}")
    except Exception as e:  # noqa: BLE001 — notification must never break the caller
        logger.debug("notify_critical: swallowed notification error: %s", e)
