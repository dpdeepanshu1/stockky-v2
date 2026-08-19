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

try:
    import kv_cache as _kv
except Exception:
    _kv = None  # type: ignore

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
    # CallMeBot Telegram API — user is @username (or phone). Optional apikey if required by account.
    # Primary user:
    "callmebot_user": os.getenv("CALLMEBOT_USER", os.getenv("CALLMEBOT_PHONE", "")),
    "callmebot_apikey": os.getenv("CALLMEBOT_APIKEY", ""),
    # Up to 5 users total — CSV of @user or @user:apikey
    # e.g. "@dpdeep29,@friend2,@user3:optionalkey"
    "callmebot_users": os.getenv("CALLMEBOT_USERS", ""),
    # Legacy aliases kept for older configs
    "callmebot_phone": os.getenv("CALLMEBOT_PHONE", ""),
    "enabled": {
        "discord": bool(os.getenv("DISCORD_WEBHOOK_URL")),
        "slack": bool(os.getenv("SLACK_WEBHOOK_URL")),
        "telegram": bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")),
        "callmebot": bool(
            os.getenv("CALLMEBOT_USER")
            or os.getenv("CALLMEBOT_PHONE")
            or os.getenv("CALLMEBOT_USERS")
        ),
    },
}

_redis = None
_USE_REDIS = os.getenv("USE_REDIS", "0").lower() in ("1", "true", "yes")
if os.getenv("DISABLE_UPSTASH", "0").lower() in ("1", "true", "yes"):
    _USE_REDIS = False
if _USE_REDIS and Redis is not None:
    try:
        url = os.getenv("UPSTASH_REDIS_REST_URL")
        token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
        if url and token:
            _redis = Redis(url=url, token=token)
            _redis.ping()
            logger.info("Connected to Upstash Redis (USE_REDIS=1)")
    except Exception as e:
        logger.warning("Redis unavailable — using Neon/memory: %s", e)
        _redis = None
else:
    logger.info("Notification Redis OFF (USE_REDIS=0) — Neon/memory config")

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
    if _kv is not None:
        try:
            val = _kv.get(CONFIG_KEY)
            if val:
                cfg = val if isinstance(val, dict) else json.loads(val)
                # Backfill any keys added in later versions.
                for k, v in ENV_DEFAULTS.items():
                    cfg.setdefault(k, v)
                return cfg
        except Exception as e:
            logger.warning("Failed to load notification config from Neon: %s", e)
    if _memory_config is not None:
        return _memory_config
    return dict(ENV_DEFAULTS)


def _save_config(cfg: dict):
    global _memory_config
    _memory_config = cfg
    if _kv is not None:
        try:
            _kv.set(CONFIG_KEY, cfg, ttl=None)
            return
        except Exception as e:
            logger.warning("Failed to persist notification config to Neon: %s", e)
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
    callmebot_user: Optional[str] = None
    callmebot_phone: Optional[str] = None  # legacy alias for user
    callmebot_apikey: Optional[str] = None
    callmebot_users: Optional[str] = None  # up to 5: "@u1,@u2" or "@u1:key,@u2"
    enabled: Optional[dict] = None  # e.g. {"discord": true, "telegram": false, "callmebot": true}


class NotifyRequest(BaseModel):
    title: str
    message: str
    urgency: str = "normal"  # "normal" | "high" — high could map to @here/@channel later
    channel: str = "all"     # "all", "telegram", "discord", "slack", "callmebot"


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
        "callmebot": {
            "configured": bool(
                (cfg.get("callmebot_user") or cfg.get("callmebot_phone") or "").strip()
                or (cfg.get("callmebot_users") or "").strip()
            ),
            "enabled": bool(enabled.get("callmebot")),
            "masked": _mask(cfg.get("callmebot_apikey", "") or "none"),
            "user": cfg.get("callmebot_user") or cfg.get("callmebot_phone") or "",
            "phone": cfg.get("callmebot_phone") or cfg.get("callmebot_user") or "",
            "users_preview": (cfg.get("callmebot_users") or ""),
            "users": (cfg.get("callmebot_users") or ""),
            "recipients_count": len(_callmebot_recipients(cfg)),
        },
        "persisted": bool(_kv is not None or _redis),
        "persist_backend": (
            "neon" if _kv is not None else ("redis" if _redis else "memory")
        ),
    }


@app.get("/health")
def health(warm: bool = False):
    out = {
        "status": "ok",
        "service": "notification-service",
        "redis": bool(_redis),
        "neon": bool(_kv is not None),
        "persisted": bool(_kv is not None or _redis),
    }
    if warm:
        if _redis:
            try:
                _redis.ping()
                out["warmed"] = True
                # Opportunistic outbox drain on keep-warm pings
                try:
                    out["outbox"] = process_outbox(max_items=10)
                except Exception as e:
                    out["outbox_error"] = str(e)[:80]
            except Exception as e:
                out["warmed"] = False
                out["warm_error"] = str(e)[:80]
        else:
            out["warmed"] = False
            out["redis"] = False
            out["note"] = "Set UPSTASH_REDIS_REST_URL + TOKEN for outbox persistence"
    return out


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
    if update.callmebot_user is not None:
        u = update.callmebot_user.strip()
        if u and not u.startswith("@") and not u[0].isdigit():
            u = "@" + u
        cfg["callmebot_user"] = u
        cfg["callmebot_phone"] = u  # keep legacy field in sync
    if update.callmebot_phone is not None and update.callmebot_user is None:
        u = update.callmebot_phone.strip()
        if u and not u.startswith("@") and u and not u[0].isdigit() and ":" not in u:
            # treat as username without @
            if not u.replace("+", "").isdigit():
                u = "@" + u.lstrip("@")
        cfg["callmebot_user"] = u
        cfg["callmebot_phone"] = u
    if update.callmebot_apikey is not None:
        cfg["callmebot_apikey"] = update.callmebot_apikey.strip()
    if update.callmebot_users is not None:
        # Cap at 5 entries
        parts = [x.strip() for x in update.callmebot_users.split(",") if x.strip()]
        cfg["callmebot_users"] = ",".join(parts[:5])

    enabled = dict(cfg.get("enabled", {}))
    if update.enabled:
        for channel, val in update.enabled.items():
            if channel in ("discord", "slack", "telegram", "callmebot"):
                enabled[channel] = bool(val)
    cfg["enabled"] = enabled

    _save_config(cfg)
    return _public_config(cfg)


@app.delete("/config/{channel}")
def clear_channel(channel: str):
    if channel not in ("discord", "slack", "telegram", "callmebot"):
        raise HTTPException(status_code=404, detail="Unknown channel")
    cfg = _load_config()
    if channel == "discord":
        cfg["discord_webhook_url"] = ""
    elif channel == "slack":
        cfg["slack_webhook_url"] = ""
    elif channel == "callmebot":
        cfg["callmebot_phone"] = ""
        cfg["callmebot_user"] = ""
        cfg["callmebot_apikey"] = ""
        cfg["callmebot_users"] = ""
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


def _callmebot_recipients(cfg: dict):
    """Up to 5 CallMeBot users. Each entry is (user, apikey_or_empty).

    User format from CallMeBot Telegram activation:
      @dpdeep29
    Optional apikey if your bot requires it:
      @dpdeep29:YOURKEY
    Phone format also accepted:
      9198XXXXXXXX
    """
    users = []
    seen = set()

    def _add(raw_user: str, key: str = ""):
        u = (raw_user or "").strip()
        if not u:
            return
        # Normalize telegram username
        if u.startswith("@"):
            pass
        elif u.replace("+", "").isdigit():
            u = u.lstrip("+")
        else:
            u = "@" + u.lstrip("@")
        if u.lower() in seen:
            return
        seen.add(u.lower())
        users.append((u, (key or "").strip()))

    primary = (cfg.get("callmebot_user") or cfg.get("callmebot_phone") or "").strip()
    primary_key = (cfg.get("callmebot_apikey") or "").strip()
    if primary:
        if ":" in primary and not primary.startswith("@"):
            a, b = primary.split(":", 1)
            _add(a, b)
        else:
            _add(primary, primary_key)

    raw = (cfg.get("callmebot_users") or "").strip()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            # @user:key or phone:key
            a, b = part.split(":", 1)
            _add(a, b)
        else:
            _add(part, "")
        if len(users) >= 5:
            break

    return users[:5]


def _send_callmebot(cfg: dict, title: str, message: str, voice_first: bool = True):
    """Send CallMeBot alert to every configured user (primary + extras, max 5).

    Priority for urgent alerts (voice_first=True, default):
      1. Telegram Voice Call via start.php
      2. Only if voice fails → text via text.php

    Each Telegram user must have activated CallMeBot themselves
    (https://www.callmebot.com/telegram-call-api/). You cannot call a
    username that never started the bot.

    Official URLs:
      start.php?user=@name&text=...  → voice call
      text.php?user=@name&text=...   → text message
    """
    import urllib.parse
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not cfg.get("enabled", {}).get("callmebot"):
        return None
    users = _callmebot_recipients(cfg)
    if not users:
        return "not configured — set CallMeBot user like @dpdeep29"

    text_body = f"{title}. {message}"
    if len(text_body) > 200:
        text_body = text_body[:197] + "..."

    def _one(user: str, apikey: str) -> str:
        q = urllib.parse.quote(text_body)
        uq = urllib.parse.quote(user)
        text_url = f"https://api.callmebot.com/text.php?user={uq}&text={q}"
        call_url = f"https://api.callmebot.com/start.php?user={uq}&text={q}"
        if apikey:
            text_url += f"&apikey={urllib.parse.quote(apikey)}"
            call_url += f"&apikey={urllib.parse.quote(apikey)}"
        # Voice call FIRST for urgent alerts; text only as fallback
        order = (("call", call_url), ("text", text_url)) if voice_first else (("text", text_url), ("call", call_url))
        last = ""
        for kind, url in order:
            try:
                resp = httpx.get(url, timeout=35)
                last = (resp.text or "")[:120]
                if resp.status_code == 200 and "error" not in last.lower():
                    return f"{user}:ok({kind})"
                if resp.status_code == 200:
                    if "not authorized" in last.lower() or "start the bot" in last.lower():
                        return f"{user}:need_activate ({last})"
                    return f"{user}:ok({kind})"
                last = f"HTTP {resp.status_code} {last}"
            except httpx.TimeoutException:
                last = f"{kind}-timeout"
            except Exception as e:
                last = str(e)[:80]
        return f"{user}:fail ({last})"

    # Primary first (index 0), then extras — sequential with short gap so CallMeBot
    # rate limits are less likely to drop secondary users. One retry per user.
    results = []
    import time as _t

    def _one_with_retry(user: str, apikey: str) -> str:
        r = _one(user, apikey)
        if ":ok(" in r or r.endswith(":ok"):
            return r
        _t.sleep(1.2)
        return _one(user, apikey)

    for i, (u, k) in enumerate(users):
        try:
            results.append(_one_with_retry(u, k))
        except Exception as e:
            results.append(f"{u}:error:{e}")
        if i < len(users) - 1:
            _t.sleep(0.8)

    any_sent = any(":ok(" in r or r.endswith(":ok") for r in results)
    summary = "; ".join(results)
    if any_sent:
        return "sent: " + summary
    return "failed: " + summary



OUTBOX_KEY = "stockky:notify:outbox"
OUTBOX_MAX_ATTEMPTS = 5
OUTBOX_BACKOFF_SEC = [30, 60, 120, 300, 600]


def _outbox_enqueue(title: str, message: str, channel: str, urgency: str, last_error: str = None):
    """Persist failed alerts for retry (Redis list). Survives process restart."""
    if not _redis:
        return None
    import time as _time
    import uuid as _uuid
    item = {
        "id": _uuid.uuid4().hex[:12],
        "title": title,
        "message": message,
        "channel": channel or "all",
        "urgency": urgency or "normal",
        "attempts": 0,
        "created_at": _time.time(),
        "next_attempt_at": _time.time(),
        "last_error": last_error,
    }
    try:
        _redis.lpush(OUTBOX_KEY, json.dumps(item))
        _redis.ltrim(OUTBOX_KEY, 0, 199)  # keep last 200
        logger.info("Outbox enqueued alert %s", item["id"])
        return item["id"]
    except Exception as e:
        logger.warning("Outbox enqueue failed: %s", e)
        return None


def _outbox_list():
    if not _redis:
        return []
    try:
        raw = _redis.lrange(OUTBOX_KEY, 0, 199) or []
        items = []
        for r in raw:
            try:
                items.append(json.loads(r) if isinstance(r, str) else r)
            except Exception:
                continue
        return items
    except Exception:
        return []


def _outbox_save_all(items: list):
    if not _redis:
        return
    try:
        _redis.delete(OUTBOX_KEY)
        for it in reversed(items):
            _redis.lpush(OUTBOX_KEY, json.dumps(it))
        _redis.ltrim(OUTBOX_KEY, 0, 199)
    except Exception as e:
        logger.warning("Outbox save failed: %s", e)


def process_outbox(max_items: int = 20) -> dict:
    """Retry due outbox items. Call from /outbox/process or external cron."""
    import time as _time
    items = _outbox_list()
    if not items:
        return {"processed": 0, "delivered": 0, "remaining": 0}
    now = _time.time()
    delivered = 0
    processed = 0
    remaining = []
    for item in items:
        if processed >= max_items:
            remaining.append(item)
            continue
        if float(item.get("next_attempt_at") or 0) > now:
            remaining.append(item)
            continue
        processed += 1
        attempts = int(item.get("attempts") or 0)
        results = _dispatch(
            item.get("title") or "Stockky",
            item.get("message") or "",
            item.get("channel") or "all",
            urgency=item.get("urgency") or "normal",
        )
        ok = any(isinstance(v, str) and str(v).startswith("sent") for v in (results or {}).values())
        if ok:
            delivered += 1
            continue
        attempts += 1
        if attempts >= OUTBOX_MAX_ATTEMPTS:
            logger.error("Outbox drop %s after %s attempts", item.get("id"), attempts)
            continue
        item["attempts"] = attempts
        backoff = OUTBOX_BACKOFF_SEC[min(attempts - 1, len(OUTBOX_BACKOFF_SEC) - 1)]
        item["next_attempt_at"] = now + backoff
        item["last_error"] = str(results)
        remaining.append(item)
    _outbox_save_all(remaining)
    return {"processed": processed, "delivered": delivered, "remaining": len(remaining)}


def _dispatch(title: str, message: str, channel_filter: str, urgency: str = "normal"):
    """Dispatch notifications.

    For urgency=high / urgent:
      1. Try CallMeBot Telegram Voice Call first.
      2. Only if voice call fails, fall back to Telegram text / Discord / Slack / CallMeBot text.
    For normal urgency: send to all matching enabled channels (CallMeBot still prefers voice).
    """
    cfg = _load_config()
    results = {}
    is_urgent = (urgency or "").lower() in ("high", "urgent", "critical")

    if is_urgent and channel_filter in ("all", "callmebot"):
        # Priority path: voice call first
        call_cfg = dict(cfg)
        enabled = dict(call_cfg.get("enabled") or {})
        # Temporarily ensure callmebot is attempted for urgent path
        if enabled.get("callmebot") or _callmebot_recipients(cfg):
            enabled["callmebot"] = True
            call_cfg["enabled"] = enabled
            voice_result = _send_callmebot(call_cfg, title, message, voice_first=True)
            if voice_result is not None:
                results["callmebot"] = voice_result
            voice_ok = isinstance(voice_result, str) and voice_result.startswith("sent")
            if voice_ok:
                # Voice succeeded — still optionally send text channels for audit trail
                # but primary delivery is done. Continue to other channels only if filter is "all".
                if channel_filter != "all":
                    return results
            # Voice failed or not configured → fall through to text channels

    channels_to_send = []
    if channel_filter == "all" or channel_filter == "telegram":
        channels_to_send.append("telegram")
    if channel_filter == "all" or channel_filter == "discord":
        channels_to_send.append("discord")
    if channel_filter == "all" or channel_filter == "slack":
        channels_to_send.append("slack")
    # For non-urgent, or urgent where voice already attempted, still try callmebot if requested
    if not is_urgent and (channel_filter == "all" or channel_filter == "callmebot"):
        channels_to_send.append("callmebot")
    elif is_urgent and channel_filter == "callmebot" and "callmebot" not in results:
        channels_to_send.append("callmebot")

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
        elif ch == "callmebot":
            result = _send_callmebot(cfg, title, message, voice_first=True)
            if result is not None:
                results["callmebot"] = result

    return results


@app.post("/notify")
def notify(req: NotifyRequest):
    attempted = _dispatch(req.title, req.message, req.channel, urgency=req.urgency or "normal")
    if not attempted:
        oid = _outbox_enqueue(req.title, req.message, req.channel, req.urgency or "normal", "no channel")
        return {
            "delivered": False,
            "outbox_id": oid,
            "note": f"No notification channel matched filter '{req.channel}' and is enabled/configured.",
        }
    delivered = any(isinstance(v, str) and v.startswith("sent") for v in attempted.values())
    note_parts = []
    for ch, result in attempted.items():
        if isinstance(result, str) and result.startswith("sent"):
            note_parts.append(f"{ch}: ok")
        else:
            note_parts.append(f"{ch}: {result}")
    note = "; ".join(note_parts)
    outbox_id = None
    if not delivered:
        outbox_id = _outbox_enqueue(
            req.title, req.message, req.channel, req.urgency or "normal", note
        )
    return {
        "delivered": delivered,
        "results": attempted,
        "note": note,
        "urgency": req.urgency,
        "outbox_id": outbox_id,
    }


@app.get("/outbox")
def outbox_status():
    items = _outbox_list()
    return {"count": len(items), "items": items[:50], "redis": bool(_redis)}


@app.post("/outbox/process")
def outbox_process(max_items: int = 20):
    return process_outbox(max_items=max_items)


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
    delivered = any(isinstance(v, str) and v.startswith("sent") for v in attempted.values())
    note_parts = []
    for ch, result in attempted.items():
        if result == "sent":
            note_parts.append(f"{ch}: ok")
        else:
            note_parts.append(f"{ch}: {result}")
    note = "; ".join(note_parts)
    return {"delivered": delivered, "results": attempted, "note": note}


# ── Neon keep-alive (every ~4 minutes) — prevents free-tier auto-suspend ──
_NEON_KEEPALIVE_SEC = int(os.getenv("NEON_KEEPALIVE_INTERVAL_SEC", "240"))
_neon_keepalive_task = None


def _neon_select_1() -> dict:
    """Lightweight SELECT 1 against Neon via shared kv_cache helpers."""
    try:
        if _kv is not None:
            eng = None
            if hasattr(_kv, "_get_neon"):
                eng = _kv._get_neon()
            if eng is not None:
                from sqlalchemy import text

                with eng.connect() as conn:
                    conn.execute(text("SELECT 1"))
                return {"ok": True, "source": "kv_cache"}
        # Fallback: direct DATABASE_URL
        url = (
            os.getenv("CACHE_DATABASE_URL")
            or os.getenv("DATABASE_URL")
            or os.getenv("TRAINING_DATABASE_URL")
        )
        if not url:
            return {"ok": False, "error": "no_database_url"}
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        from sqlalchemy import create_engine, text

        eng = create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return {"ok": True, "source": "direct"}
    except Exception as e:
        logger.debug("neon keepalive failed: %s", e)
        return {"ok": False, "error": str(e)[:160]}


@app.get("/ops/neon-keepalive")
@app.post("/ops/neon-keepalive")
def neon_keepalive_endpoint():
    """Cron-friendly keep-alive (also run in-process every ~4 min)."""
    return _neon_select_1()


@app.on_event("startup")
async def _start_neon_keepalive_loop():
    """Background loop so Neon stays warm even without external cron."""
    import asyncio

    global _neon_keepalive_task

    async def _loop():
        # Stagger first ping slightly after boot
        await asyncio.sleep(15)
        while True:
            try:
                result = await asyncio.get_event_loop().run_in_executor(None, _neon_select_1)
                if result.get("ok"):
                    logger.info("Neon keep-alive OK (%s)", result.get("source"))
                else:
                    logger.debug("Neon keep-alive skip/fail: %s", result.get("error"))
            except Exception as e:
                logger.debug("Neon keep-alive loop: %s", e)
            await asyncio.sleep(max(60, _NEON_KEEPALIVE_SEC))

    try:
        _neon_keepalive_task = asyncio.create_task(_loop())
        logger.info("Neon keep-alive loop started (every %ss)", _NEON_KEEPALIVE_SEC)
    except Exception as e:
        logger.warning("Could not start Neon keep-alive loop: %s", e)


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
    """Manual Call Me Now — always prefers Telegram Voice Call first, then text fallback."""
    cfg = _load_config()
    cfg = dict(cfg)
    enabled = dict(cfg.get("enabled") or {})
    enabled["callmebot"] = True
    cfg["enabled"] = enabled
    result = _send_callmebot(cfg, "Stockky Call Alert", message, voice_first=True)
    return {"ok": isinstance(result, str) and result.startswith("sent"), "result": result}
