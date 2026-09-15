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
"""
from __future__ import annotations

import logging
import re as _re

import httpx

import config

logger = logging.getLogger("position-stocks-notifier")

_NOTIFICATION_SERVICE_URL = f"{config.NOTIFICATION_SERVICE_URL}"


def notify_sync(text: str) -> bool:
    """Synchronous, best-effort. Tries notification-scheduler-service
    first (uses the Alert panel's saved Telegram config), falls back to
    a direct Telegram call using env vars. Never raises."""
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
