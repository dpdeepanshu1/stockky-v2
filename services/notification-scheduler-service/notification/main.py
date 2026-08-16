"""
Notification Service
-----------------------
Single responsibility: deliver an alert when — and only when — something
actionable changed, per the spec's "no unnecessary notifications" rule:
  - A new BUY NOW opportunity appears
  - An existing BUY flips to SELL
  - A tracked event changes a recommendation

Delivery channels are free webhooks — Discord and Slack both offer free
incoming webhooks with no API key/billing account needed; Telegram bots
are also free.

Channel credentials can be configured two ways:
  1. Environment variables (DISCORD_WEBHOOK_URL, SLACK_WEBHOOK_URL,
     TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) — used as the initial default.
  2. The webpage's Notifications tab, via GET/POST /config — this is
     persisted in Upstash Redis (same store the API Gateway uses for the
     watchlist) and takes priority over env vars once saved, so nobody
     needs to touch code or redeploy to add/change a channel.

This service does not decide *what* counts as notification-worthy — the
Scheduler Service (which already tracks previous vs current scan results)
calls POST /notify with a pre-built message. Keeping that decision in the
Scheduler avoids a circular dependency and keeps this service a dumb,
reliable delivery pipe — same design principle as Market Data Service.

v0.5.0 – respects the 'channel' parameter: "telegram", "discord", "slack", or "all".
"""
import os
import json
import logging
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

try:
    from upstash_redis import Redis
except ImportError:  # pragma: no cover - optional dep during local dev
    Redis = None  # type: ignore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("notification-service")

app = FastAPI(title="Stockky Notification Service", version="0.5.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

CONFIG_KEY = "stockky:notification_config"

# Env vars are only the *initial* defaults — the webpage config overrides them.
ENV_DEFAULTS = {
    "discord_webhook_url": os.getenv("DISCORD_WEBHOOK_URL", ""),
    "slack_webhook_url": os.getenv("SLACK_WEBHOOK_URL", ""),
    "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
    "enabled": {
        "discord": bool(os.getenv("DISCORD_WEBHOOK_URL")),
        "slack": bool(os.getenv("SLACK_WEBHOOK_URL")),
        "telegram": bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")),
    },
}

_redis = None
if Redis is not None:
    try:
        _redis = Redis(
            url=os.getenv("UPSTASH_REDIS_REST_URL"),
            token=os.getenv("UPSTASH_REDIS_REST_TOKEN"),
        )
        _redis.ping()
        logger.info("Connected to Upstash Redis")
    except Exception as e:
        logger.warning("Redis unavailable — notification config will not persist across restarts: %s", e)
        _redis = None

# In-memory fallback so the service still works (for the current process
# lifetime) when Redis isn't configured, e.g. local `docker compose up`.
_memory_config: Optional[dict] = None


@app.get("/")
def root():
    return {
        "service": "Stockky Notification Service",
        "version": "0.5.0",
        "status": "running",
        "endpoints": {
            "/health": "GET – health check",
            "/config": "GET – current config / POST – update config",
            "/config/{channel}": "DELETE – clear a channel",
            "/notify": "POST – send a notification",
            "/test": "POST – test notification delivery",
            "/docs": "Swagger UI documentation",
        },
    }


def _load_config() -> dict:
    global _memory_config
    if _redis:
        try:
            val = _redis.get(CONFIG_KEY)
            if val:
                cfg = json.loads(val)
                # Backfill any keys added in later versions.
                for k, v in ENV_DEFAULTS.items():
                    cfg.setdefault(k, v)
                return cfg
        except Exception as e:
            logger.warning("Failed to load notification config from Redis: %s", e)
    if _memory_config is not None:
        return _memory_config
    return dict(ENV_DEFAULTS)


def _save_config(cfg: dict):
    global _memory_config
    _memory_config = cfg
    if _redis:
        try:
            _redis.set(CONFIG_KEY, json.dumps(cfg))
            return
        except Exception as e:
            logger.warning("Failed to persist notification config to Redis: %s", e)


def _mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "•" * len(secret)
    return f"{secret[:4]}…{secret[-4:]}"


class ChannelUpdate(BaseModel):
    discord_webhook_url: Optional[str] = None
    slack_webhook_url: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    enabled: Optional[dict] = None  # e.g. {"discord": true, "telegram": false}


class NotifyRequest(BaseModel):
    title: str
    message: str
    urgency: str = "normal"  # "normal" | "high" — high could map to @here/@channel later
    channel: str = "all"     # "all", "telegram", "discord", "slack"


def _public_config(cfg: dict) -> dict:
    enabled = cfg.get("enabled", {})
    return {
        "discord": {
            "configured": bool(cfg.get("discord_webhook_url")),
            "enabled": bool(enabled.get("discord")),
            "masked": _mask(cfg.get("discord_webhook_url", "")),
        },
        "slack": {
            "configured": bool(cfg.get("slack_webhook_url")),
            "enabled": bool(enabled.get("slack")),
            "masked": _mask(cfg.get("slack_webhook_url", "")),
        },
        "telegram": {
            "configured": bool(cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id")),
            "enabled": bool(enabled.get("telegram")),
            "masked": _mask(cfg.get("telegram_bot_token", "")),
            "chat_id": cfg.get("telegram_chat_id", ""),
        },
        "persisted": bool(_redis),
    }


@app.get("/health")
def health():
    cfg = _load_config()
    return {
        "status": "ok",
        "service": "notification-service",
        "redis": bool(_redis),
        "channels_configured": {
            "discord": bool(cfg.get("discord_webhook_url")),
            "slack": bool(cfg.get("slack_webhook_url")),
            "telegram": bool(cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id")),
        },
    }


@app.get("/config")
def get_config():
    return _public_config(_load_config())


@app.post("/config")
def update_config(update: ChannelUpdate):
    cfg = _load_config()

    if update.discord_webhook_url is not None:
        cfg["discord_webhook_url"] = update.discord_webhook_url.strip()
    if update.slack_webhook_url is not None:
        cfg["slack_webhook_url"] = update.slack_webhook_url.strip()
    if update.telegram_bot_token is not None:
        cfg["telegram_bot_token"] = update.telegram_bot_token.strip()
    if update.telegram_chat_id is not None:
        cfg["telegram_chat_id"] = update.telegram_chat_id.strip()

    enabled = dict(cfg.get("enabled", {}))
    if update.enabled:
        for channel, val in update.enabled.items():
            if channel in ("discord", "slack", "telegram"):
                enabled[channel] = bool(val)
    cfg["enabled"] = enabled

    _save_config(cfg)
    return _public_config(cfg)


@app.delete("/config/{channel}")
def clear_channel(channel: str):
    if channel not in ("discord", "slack", "telegram"):
        raise HTTPException(status_code=404, detail="Unknown channel")
    cfg = _load_config()
    if channel == "discord":
        cfg["discord_webhook_url"] = ""
    elif channel == "slack":
        cfg["slack_webhook_url"] = ""
    else:
        cfg["telegram_bot_token"] = ""
        cfg["telegram_chat_id"] = ""
    cfg.setdefault("enabled", {})[channel] = False
    _save_config(cfg)
    return _public_config(cfg)


def _send_discord(cfg: dict, title: str, message: str):
    url = cfg.get("discord_webhook_url")
    if not (url and cfg.get("enabled", {}).get("discord")):
        return None
    payload = {"content": f"**{title}**\n{message}"}
    try:
        resp = httpx.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return "sent"
    except httpx.HTTPError as e:
        logger.error("Discord notification failed: %s", e)
        return f"failed: {e}"


def _send_slack(cfg: dict, title: str, message: str):
    url = cfg.get("slack_webhook_url")
    if not (url and cfg.get("enabled", {}).get("slack")):
        return None
    payload = {"text": f"*{title}*\n{message}"}
    try:
        resp = httpx.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return "sent"
    except httpx.HTTPError as e:
        logger.error("Slack notification failed: %s", e)
        return f"failed: {e}"


def _send_telegram(cfg: dict, title: str, message: str):
    token = cfg.get("telegram_bot_token")
    chat_id = cfg.get("telegram_chat_id")
    enabled = cfg.get("enabled", {}).get("telegram")

    if not token or not chat_id:
        logger.warning("Telegram token or chat ID missing – cannot send notification")
        return "not configured (missing token or chat ID)"
    if not enabled:
        logger.info("Telegram channel is disabled – skipping")
        return "not sent (disabled)"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": f"*{title}*\n\n{message}",
        "parse_mode": "Markdown",
    }
    try:
        resp = httpx.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info("Telegram notification sent successfully")
            return "sent"
        else:
            logger.error(f"Telegram API error: {resp.status_code} - {resp.text[:200]}")
            return f"failed: HTTP {resp.status_code}"
    except httpx.HTTPError as e:
        logger.error("Telegram notification failed: %s", e)
        return f"failed: {e}"


def _dispatch(title: str, message: str, channel_filter: str):
    cfg = _load_config()
    results = {}
    # Determine which channels to send to based on the filter
    channels_to_send = []
    if channel_filter == "all" or channel_filter == "telegram":
        channels_to_send.append("telegram")
    if channel_filter == "all" or channel_filter == "discord":
        channels_to_send.append("discord")
    if channel_filter == "all" or channel_filter == "slack":
        channels_to_send.append("slack")

    # If the filter is a specific channel, only send to that one (already covered above)
    # Also ensure we don't send duplicates if "all" includes a specific channel

    for ch in channels_to_send:
        if ch == "telegram":
            result = _send_telegram(cfg, title, message)
            if result is not None:
                results["telegram"] = result
        elif ch == "discord":
            result = _send_discord(cfg, title, message)
            if result is not None:
                results["discord"] = result
        elif ch == "slack":
            result = _send_slack(cfg, title, message)
            if result is not None:
                results["slack"] = result

    return results


@app.post("/notify")
def notify(req: NotifyRequest):
    attempted = _dispatch(req.title, req.message, req.channel)
    if not attempted:
        return {
            "delivered": False,
            "note": f"No notification channel matched filter '{req.channel}' and is enabled/configured.",
        }
    # Check if any channel succeeded
    delivered = any(v == "sent" for v in attempted.values())
    # Build a note with the results
    note_parts = []
    for ch, result in attempted.items():
        if result == "sent":
            note_parts.append(f"{ch}: ok")
        else:
            note_parts.append(f"{ch}: {result}")
    note = "; ".join(note_parts)
    return {"delivered": delivered, "results": attempted, "note": note}


@app.post("/test")
def test_notifications():
    # Test sends to all enabled channels
    attempted = _dispatch(
        "✅ Stockky test notification",
        "If you can see this, the channel is wired up correctly.",
        channel_filter="all"
    )
    if not attempted:
        return {
            "delivered": False,
            "note": "No channel is both configured and enabled. Save credentials and turn the toggle on first.",
        }
    delivered = any(v == "sent" for v in attempted.values())
    note_parts = []
    for ch, result in attempted.items():
        if result == "sent":
            note_parts.append(f"{ch}: ok")
        else:
            note_parts.append(f"{ch}: {result}")
    note = "; ".join(note_parts)
    return {"delivered": delivered, "results": attempted, "note": note}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8008))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)

# ── CallMeBot free Telegram voice call ─────────────────────────────────────
# Docs: https://www.callmebot.com/blog/free-api-telegram-bot/
# Env: CALLMEBOT_PHONE=+91xxxxxxxxxx  CALLMEBOT_APIKEY=xxxxx
# Multi-user: CALLMEBOT_USERS=phone1:apikey1,phone2:apikey2
import urllib.parse

def _callmebot_users():
    users = []
    single_phone = os.getenv("CALLMEBOT_PHONE")
    single_key = os.getenv("CALLMEBOT_APIKEY")
    if single_phone and single_key:
        users.append((single_phone, single_key))
    raw = os.getenv("CALLMEBOT_USERS", "")
    for part in raw.split(","):
        part = part.strip()
        if ":" in part:
            ph, key = part.split(":", 1)
            users.append((ph.strip(), key.strip()))
    return users

@app.post("/call/me")
@app.get("/call/me")
def call_me_now(message: str = "Stockky alert: action required on your picks"):
    """Manual Call Me Now — free CallMeBot Telegram call/text."""
    users = _callmebot_users()
    if not users:
        return {"ok": False, "error": "Configure CALLMEBOT_PHONE + CALLMEBOT_APIKEY (or CALLMEBOT_USERS)"}
    results = []
    for phone, apikey in users:
        try:
            text = urllib.parse.quote(message)
            url = f"https://api.callmebot.com/start.php?source=auth&user={urllib.parse.quote(phone)}&text={text}&apikey={apikey}"
            # voice-ish path also supported by some CallMeBot endpoints
            r = httpx.get(url, timeout=20)
            results.append({"phone": phone, "status": r.status_code, "body": r.text[:200]})
        except Exception as e:
            results.append({"phone": phone, "error": str(e)})
    return {"ok": True, "results": results}
