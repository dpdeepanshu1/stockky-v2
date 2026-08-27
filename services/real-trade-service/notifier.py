"""
notifier.py — direct Telegram notifications for this service's own
events (manual/auto BUY sent, broker fill confirmed, exit sent, auto-pilot
cycle summary). Separate from notification-scheduler-service, which
notifies about scan/candidate opportunities, not real order/position
events — see config.py's TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID docstring.

Deliberately dumb and best-effort: a Telegram outage or missing config
must NEVER block or fail an order path. Every function here swallows its
own exceptions and just logs.
"""
from __future__ import annotations

import logging

import httpx

import config

logger = logging.getLogger("real-trade-notifier")


def is_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


async def notify_async(text: str) -> bool:
    """Fire-and-forget Telegram message. Returns False (never raises) if
    Telegram isn't configured or the send failed — callers should treat
    this purely as a side effect, not something to branch on."""
    if not is_configured():
        logger.debug("Telegram not configured — skipping notify: %s", text[:80])
        return False
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code != 200:
            logger.warning("Telegram notify failed (%s): %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:
        logger.warning("Telegram notify error: %s", e)
        return False


def notify_sync(text: str) -> bool:
    """Synchronous variant for call sites that aren't already in an
    async function (e.g. manual_engine's exception handlers)."""
    if not is_configured():
        return False
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = httpx.post(url, json=payload, timeout=15.0)
        if resp.status_code != 200:
            logger.warning("Telegram notify failed (%s): %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:
        logger.warning("Telegram notify error: %s", e)
        return False
