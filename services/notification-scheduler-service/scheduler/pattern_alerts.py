"""pattern_alerts.py - E29: one daily Telegram / Discord / Slack digest of fresh chart signals on the user's WATCHLIST.

Three kinds of signal, all read from the `events` list of the daily chart-context the gateway already serves (`/stock/{sym}/chart-context`, context only):
  * breakout   a chart pattern whose status is `confirmed` (price closed beyond the line by 0.25 ATR) - bullish = breakout, bearish = breakdown
  * cross      a golden / death cross (SMA50/200 or SMA20/50) that happened in the last CROSS_MAX_AGE daily bars
  * squeeze    the Bollinger squeeze switched on, or just released up / down

ALERT ONLY. Nothing here places, blocks or sizes a trade; the signals have not passed the B-tools yet, so the message says so.
One digest per day after the close (PATTERN_ALERTS_TIME, default 16:05 IST), capped at PATTERN_ALERTS_MAX_LINES lines. A signal that was already
sent is not sent again for its quiet period (breakout 10 days, cross 5, squeeze 5, release 3), kept in `<SCHEDULER_STATE_DIR>/pattern_alerts_state.json`
(the compose /data volume). A signal is marked as sent only after the notification service accepted the digest. Any failure = no alert, never a crash.

    GET  /scheduler/pattern-alerts/status
    POST /scheduler/pattern-alerts/run?dry_run=true     (default: only lists what WOULD be sent; dry_run=false sends)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("pattern-alerts")
IST = ZoneInfo("Asia/Kolkata")

STATE_DIR = (os.getenv("SCHEDULER_STATE_DIR") or "").strip() or "/data"
STATE_PATH = os.path.join(STATE_DIR, "pattern_alerts_state.json")
API_GATEWAY_URL = (os.getenv("API_GATEWAY_URL") or "").strip().rstrip("/") or "http://api-gateway:8000"

QUIET_DAYS = {"breakout": 10, "cross": 5, "squeeze": 5, "squeeze_release": 3}
CROSS_MAX_AGE = 1                       # daily bars; the digest runs after the close, so 0 = today and 1 = yesterday (covers a missed day)
_LOCK = threading.Lock()
_LAST: Dict[str, Any] = {"date": None, "status": "idle", "events": 0, "sent": 0, "symbols": 0, "error": None, "at": None}


def _env(name: str, default: str) -> str:
    return (os.getenv(name) or "").strip() or default


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(float(_env(name, str(default))))))
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    return _env("PATTERN_ALERTS_ENABLED", "true").lower() in ("1", "true", "yes", "on")


def run_time() -> str:
    t = _env("PATTERN_ALERTS_TIME", "16:05")
    try:
        h, m = t.split(":")
        return f"{int(h):02d}:{int(m):02d}" if 0 <= int(h) <= 23 and 0 <= int(m) <= 59 else "16:05"
    except (ValueError, AttributeError):
        return "16:05"


def notify_url() -> str:
    explicit = _env("PATTERN_ALERTS_NOTIFY_URL", "")
    return (explicit or f"http://127.0.0.1:{_env('PORT', '8000')}/notification").rstrip("/")


# ---------------------------------------------------------------- pure: payload -> events
def extract_events(symbol: str, payload: Any) -> List[Dict[str, Any]]:
    """Fresh alert-worthy signals in one chart-context payload. Never raises; an unusable payload gives []."""
    out: List[Dict[str, Any]] = []
    try:
        if not isinstance(payload, dict) or not payload.get("ok"):
            return out
        # `events` (uncapped, added in E29) when the technical service has it; older payloads only have the 14-badge list
        items = payload.get("events") if isinstance(payload.get("events"), list) else payload.get("badges")
        for b in items or []:
            if not isinstance(b, dict):
                continue
            kind, key, tone = b.get("kind"), str(b.get("key") or ""), b.get("tone")
            label = str(b.get("label") or key)
            if kind == "chart" and b.get("status") == "confirmed" and tone in ("good", "bad"):
                up = tone == "good"
                out.append({"symbol": symbol, "type": "breakout", "id": f"{symbol}:breakout:{key}", "tone": tone,
                            "text": f"{label} confirmed - {'breakout above' if up else 'breakdown below'} the line"})
            elif kind == "signal" and key.endswith(("golden_cross", "death_cross")):
                ago = b.get("bars_ago")
                if isinstance(ago, (int, float)) and 0 <= ago <= CROSS_MAX_AGE:
                    out.append({"symbol": symbol, "type": "cross", "id": f"{symbol}:cross:{key}", "tone": tone,
                                "text": f"{label} ({'today' if ago == 0 else 'yesterday'})"})
            elif kind == "signal" and key == "squeeze":
                out.append({"symbol": symbol, "type": "squeeze", "id": f"{symbol}:squeeze", "tone": "warn",
                            "text": "Bollinger squeeze - range is very tight, a big move may follow (direction unknown)"})
            elif kind == "signal" and key == "squeeze_release":
                out.append({"symbol": symbol, "type": "squeeze_release", "id": f"{symbol}:squeeze_release:{tone}", "tone": tone,
                            "text": f"{label} - closed outside the band after a squeeze"})
    except Exception as e:  # noqa: BLE001
        logger.debug("extract_events %s failed: %s", symbol, e)
    return out


def filter_new(events: List[Dict[str, Any]], state: Dict[str, str], today: date) -> List[Dict[str, Any]]:
    """Drop events whose id was sent within its quiet period."""
    fresh = []
    for ev in events:
        last = state.get(ev["id"])
        try:
            if last and (today - date.fromisoformat(last)).days < QUIET_DAYS.get(ev["type"], 5):
                continue
        except ValueError:
            pass
        fresh.append(ev)
    return fresh


_ORDER = {"breakout": 0, "cross": 1, "squeeze_release": 2, "squeeze": 3}
_ICON = {"good": "🟢", "bad": "🔴", "warn": "🟡"}


def format_digest(events: List[Dict[str, Any]], today: date, max_lines: int) -> Optional[Dict[str, str]]:
    """{'title', 'message'} or None when there is nothing to say. Lines are sorted by kind then symbol and cut at max_lines."""
    if not events:
        return None
    evs = sorted(events, key=lambda e: (_ORDER.get(e["type"], 9), e["symbol"]))
    lines = [f"{_ICON.get(e.get('tone'), '⚪')} {e['symbol']}: {e['text']}" for e in evs[:max_lines]]
    if len(evs) > max_lines:
        lines.append(f"... and {len(evs) - max_lines} more (open the chart of each watchlist stock)")
    msg = (f"Watchlist chart signals after the close, {today.strftime('%d %b %Y')}:\n" + "\n".join(lines) +
           "\n\nThese are chart readings only. They are not yet proven to make money, and Stockky does not trade on them.")
    return {"title": f"📈 {len(evs)} chart signal{'s' if len(evs) != 1 else ''} on your watchlist", "message": msg}


# ---------------------------------------------------------------- state (best effort)
def load_state() -> Dict[str, str]:
    try:
        with open(STATE_PATH, "r") as f:
            d = json.load(f)
        return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        logger.warning("pattern_alerts: state unreadable (%s) - starting empty", e)
        return {}


def save_state(state: Dict[str, str], today: date) -> None:
    keep = {k: v for k, v in state.items() if _recent(v, today)}
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(keep, f)
        os.replace(tmp, STATE_PATH)
    except Exception as e:  # noqa: BLE001
        logger.warning("pattern_alerts: state not saved (%s) - the same signals may repeat after a restart", e)


def _recent(v: str, today: date) -> bool:
    try:
        return (today - date.fromisoformat(v)).days <= 30
    except ValueError:
        return False


# ---------------------------------------------------------------- I/O
def fetch_watchlist() -> List[str]:
    r = httpx.get(f"{API_GATEWAY_URL}/watchlist", timeout=20)
    r.raise_for_status()
    syms = (r.json() or {}).get("symbols") or []
    seen, out = set(), []
    for s in syms:
        s = str(s).strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def fetch_chart(symbol: str) -> Any:
    r = httpx.get(f"{API_GATEWAY_URL}/stock/{symbol}/chart-context", params={"tf": "1d", "tail": 260}, timeout=35)
    return r.json() if r.status_code == 200 else None


def send(title: str, message: str) -> bool:
    try:
        r = httpx.post(f"{notify_url()}/notify", json={"title": title, "message": message, "channel": "all"}, timeout=60)
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        logger.warning("pattern_alerts: notify failed: %s", e)
        return False


def run_once(dry_run: bool = False, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Scan the watchlist and send (or, with dry_run, only list) today's digest. Never raises."""
    now = now or datetime.now(IST)
    today = now.date()
    res: Dict[str, Any] = {"ok": False, "dry_run": dry_run, "date": today.isoformat(), "symbols": 0, "events": 0, "new": 0, "sent": False,
                           "digest": None, "error": None}
    if not _LOCK.acquire(blocking=False):
        res["error"] = "already running"
        return res
    try:
        _LAST.update(status="running", error=None)
        cap = _int_env("PATTERN_ALERTS_MAX_SYMBOLS", 80, 1, 500)
        syms = fetch_watchlist()[:cap]
        res["symbols"] = len(syms)
        events: List[Dict[str, Any]] = []
        for s in syms:
            try:
                events.extend(extract_events(s, fetch_chart(s)))
            except Exception as e:  # noqa: BLE001
                logger.debug("pattern_alerts: %s skipped (%s)", s, e)
            time.sleep(0.2)                       # the gateway serves these from cache; this only keeps a cold start gentle
        res["events"] = len(events)
        state = load_state()
        new = filter_new(events, state, today)
        res["new"] = len(new)
        digest = format_digest(new, today, _int_env("PATTERN_ALERTS_MAX_LINES", 25, 3, 100))
        res["digest"] = digest
        if digest and not dry_run:
            if send(digest["title"], digest["message"]):
                for ev in new:
                    state[ev["id"]] = today.isoformat()
                save_state(state, today)
                res["sent"] = True
            else:
                res["error"] = "notification service did not accept the digest (will retry on the next run)"
        res["ok"] = res["error"] is None
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        logger.warning("pattern_alerts: run failed: %s", res["error"])
    finally:
        _LAST.update(date=res["date"] if not dry_run else _LAST.get("date"), status="idle" if res["error"] is None else "error",
                     events=res["events"], sent=res["new"] if res["sent"] else 0, symbols=res["symbols"], error=res["error"],
                     at=datetime.now(IST).isoformat(timespec="seconds"))
        if _LOCK.locked():
            _LOCK.release()
    return res


def status() -> Dict[str, Any]:
    return {"enabled": enabled(), "run_time_ist": run_time(), "max_symbols": _int_env("PATTERN_ALERTS_MAX_SYMBOLS", 80, 1, 500),
            "max_lines": _int_env("PATTERN_ALERTS_MAX_LINES", 25, 3, 100), "quiet_days": QUIET_DAYS, "last": dict(_LAST),
            "alert_only": True}


# ---------------------------------------------------------------- daily loop
def due(now: datetime, last_done: Optional[str]) -> bool:
    """True on a weekday at / after run_time() IST when today's run has not been done yet."""
    return enabled() and now.weekday() < 5 and last_done != now.date().isoformat() and now.strftime("%H:%M") >= run_time()


async def loop() -> None:
    import asyncio
    await asyncio.sleep(90)
    done: Optional[str] = _LAST.get("date")
    while True:
        try:
            now = datetime.now(IST)
            if due(now, done):
                logger.info("pattern_alerts: daily run at IST %s", now.strftime("%H:%M"))
                r = await asyncio.get_event_loop().run_in_executor(None, run_once)
                if r.get("ok"):
                    done = now.date().isoformat()          # a failed run is retried on the next tick (every 10 min) until the day ends
        except Exception as e:  # noqa: BLE001
            logger.warning("pattern_alerts: loop error: %s", e)
        await asyncio.sleep(600)
