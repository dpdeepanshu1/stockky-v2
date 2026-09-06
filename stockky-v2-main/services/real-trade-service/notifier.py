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
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

import config

logger = logging.getLogger("real-trade-notifier")

# Internal URL to notification-scheduler-service (docker-compose service name)
# Injected via NOTIFICATION_SERVICE_URL env var (added to docker-compose.yml)
# Falls back to the standard compose hostname if not set.
_NOTIFICATION_SERVICE_URL = os.getenv(
    "NOTIFICATION_SERVICE_URL",
    "http://notification-scheduler-service:8000/notification",
).rstrip("/")


def is_configured() -> bool:
    """Always True when notification-scheduler-service is reachable
    (it has its own Telegram config). Also True if env vars are set."""
    return True  # best-effort — we always try; silence is logged, not raised


async def notify_async(text: str) -> bool:
    """Fire-and-forget notification. Tries notification-scheduler-service
    first (uses Alert panel Telegram config), falls back to direct Telegram
    env-var call. Returns False (never raises) on failure."""
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
    return _direct_telegram(text)


def notify_sync(text: str) -> bool:
    """Synchronous variant for call sites that aren't in an async function
    (e.g. exit_engine). Same routing logic as notify_async."""
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
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = httpx.post(url, json=payload, timeout=15.0)
        if resp.status_code != 200:
            logger.warning("Direct Telegram notify failed (%s): %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:
        logger.warning("Direct Telegram notify error: %s", e)
        return False
