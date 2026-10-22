"""watchlist_moves.py - E28 (notification half): a message when a WATCHLIST stock has moved WATCHLIST_MOVE_ALERT_PCT (default 5 %) since it was added.

The baseline lives HERE, on the server, so the alert fires with the app closed and follows the watchlist across devices:
  * the first time the checker sees a symbol on the gateway `/watchlist` it records the current `/quote` price as its baseline (source `first_seen_server`;
    with the default 15-minute check that is within a quarter of an hour of adding it, during market hours),
  * a symbol removed from the watchlist forgets its baseline (re-adding starts fresh).
The browser card ("% since added", watchlistGain.ts) keeps its own per-device baseline; the two can differ by the minutes between add and first check.

ALERT ONLY. Nothing here places, blocks or sizes a trade.
One alert per direction: after an up-alert the symbol is quiet until the move falls back inside half the threshold (re-armed); a swing to the other side
alerts again. State: `<SCHEDULER_STATE_DIR>/watchlist_moves_state.json` (compose /data volume). A symbol is marked alerted only after the notification service
answered 200. Any failure = no alert, never a crash.

    GET  /scheduler/watchlist-moves/status
    POST /scheduler/watchlist-moves/run?dry_run=true     (default: lists what WOULD be sent; dry_run=false sends)
Switches (read at call time): WATCHLIST_MOVE_ALERTS_ENABLED (true), WATCHLIST_MOVE_ALERT_PCT (5), WATCHLIST_MOVE_CHECK_MIN (15), WATCHLIST_MOVE_MAX_SYMBOLS (80).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("watchlist-moves")
IST = ZoneInfo("Asia/Kolkata")

STATE_DIR = (os.getenv("SCHEDULER_STATE_DIR") or "").strip() or "/data"
STATE_PATH = os.path.join(STATE_DIR, "watchlist_moves_state.json")
API_GATEWAY_URL = (os.getenv("API_GATEWAY_URL") or "").strip().rstrip("/") or "http://api-gateway:8000"
NOTIFY_BASE = (os.getenv("PATTERN_ALERTS_NOTIFY_URL") or "").strip() or f"http://127.0.0.1:{(os.getenv('PORT') or '8000').strip() or '8000'}/notification"

OPEN_HHMM, CLOSE_HHMM = "09:20", "15:35"
_LOCK = threading.Lock()
_LAST: Dict[str, Any] = {"status": "idle", "symbols": 0, "alerts": 0, "sent": 0, "error": None, "at": None}


def _env(name: str, default: str) -> str:
    return (os.getenv(name) or "").strip() or default


def _num_env(name: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(_env(name, str(default)))
        return max(lo, min(hi, v)) if v == v else default
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    return _env("WATCHLIST_MOVE_ALERTS_ENABLED", "true").lower() in ("1", "true", "yes", "on")


def threshold_pct() -> float:
    return _num_env("WATCHLIST_MOVE_ALERT_PCT", 5.0, 0.5, 50.0)


def check_minutes() -> int:
    return int(_num_env("WATCHLIST_MOVE_CHECK_MIN", 15, 1, 240))


def max_symbols() -> int:
    return int(_num_env("WATCHLIST_MOVE_MAX_SYMBOLS", 80, 1, 500))


def _valid_price(p: Any) -> bool:
    return isinstance(p, (int, float)) and not isinstance(p, bool) and p == p and 0 < p < float("inf")


# ---------------------------------------------------------------- pure
def pct_since(base: Any, price: Any) -> Optional[float]:
    return round((float(price) - float(base)) / float(base) * 100.0, 2) if _valid_price(base) and _valid_price(price) else None


def evaluate(state: Dict[str, Any], symbols: List[str], prices: Dict[str, Any], now: datetime, threshold: float):
    """(new_state, alerts). Records baselines, forgets removed symbols, applies the one-alert-per-direction rule. Pure; never raises.
    state = {SYM: {"price": float, "ts": iso, "source": str, "alerted": "up" | "down" | None}}."""
    new: Dict[str, Any] = {}
    alerts: List[Dict[str, Any]] = []
    if not isinstance(state, dict):
        state = {}
    try:
        for sym in symbols:
            rec = state.get(sym) if isinstance(state.get(sym), dict) else None
            price = prices.get(sym)
            if rec is None or not _valid_price(rec.get("price")):
                if _valid_price(price):
                    new[sym] = {"price": float(price), "ts": now.isoformat(timespec="seconds"), "source": "first_seen_server", "alerted": None}
                continue                                 # no baseline yet and no price: try again next check
            rec = dict(rec)
            pct = pct_since(rec["price"], price)
            if pct is not None:
                side = "up" if pct >= threshold else ("down" if pct <= -threshold else None)
                if side and rec.get("alerted") != side:
                    alerts.append({"symbol": sym, "pct": pct, "price": float(price), "base": rec["price"], "since": rec.get("ts"), "side": side})
                elif side is None and rec.get("alerted") and abs(pct) < threshold / 2.0:
                    rec["alerted"] = None                # fell back inside half the threshold: armed again
                # between half and full threshold with an alert already sent: stays quiet
            new[sym] = rec
    except Exception as e:  # noqa: BLE001
        logger.debug("watchlist_moves.evaluate failed: %s", e)
        return dict(state), []
    return new, alerts


def format_alert(alerts: List[Dict[str, Any]], max_lines: int = 25) -> Optional[Dict[str, str]]:
    if not alerts:
        return None
    ev = sorted(alerts, key=lambda a: -abs(a["pct"]))
    lines = []
    for a in ev[:max_lines]:
        added = f" (added {a['since'][:10]})" if a.get("since") else ""
        lines.append(f"{'🟢' if a['side'] == 'up' else '🔴'} {a['symbol']}: {a['pct']:+.2f}% since added, Rs {a['base']:.2f} -> Rs {a['price']:.2f}{added}")
    if len(ev) > max_lines:
        lines.append(f"... and {len(ev) - max_lines} more")
    return {"title": f"📊 {len(ev)} watchlist stock{'s' if len(ev) != 1 else ''} moved {threshold_pct():g}%+",
            "message": "Watchlist moves since added:\n" + "\n".join(lines) + "\n\nThis is a price alert only. Stockky does not trade on it."}


def market_open(now: datetime) -> bool:
    return now.weekday() < 5 and OPEN_HHMM <= now.strftime("%H:%M") <= CLOSE_HHMM


# ---------------------------------------------------------------- state (best effort)
def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_PATH, "r") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        logger.warning("watchlist_moves: state unreadable (%s) - starting empty", e)
        return {}


def save_state(state: Dict[str, Any]) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)
    except Exception as e:  # noqa: BLE001
        logger.warning("watchlist_moves: state not saved (%s) - baselines may restart after a restart", e)


# ---------------------------------------------------------------- I/O
def fetch_watchlist() -> List[str]:
    r = httpx.get(f"{API_GATEWAY_URL}/watchlist", timeout=20)
    r.raise_for_status()
    seen, out = set(), []
    for s in (r.json() or {}).get("symbols") or []:
        s = str(s).strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def fetch_price(symbol: str) -> Optional[float]:
    try:
        r = httpx.get(f"{API_GATEWAY_URL}/quote/{symbol}", timeout=12)
        p = (r.json() or {}).get("price") if r.status_code == 200 else None
        return float(p) if _valid_price(p) else None
    except Exception:  # noqa: BLE001
        return None


def send(title: str, message: str) -> bool:
    try:
        r = httpx.post(f"{NOTIFY_BASE.rstrip('/')}/notify", json={"title": title, "message": message, "channel": "all"}, timeout=60)
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        logger.warning("watchlist_moves: notify failed: %s", e)
        return False


def run_once(dry_run: bool = False, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Record baselines, find moves and send (or with dry_run only list) one message. Never raises."""
    now = now or datetime.now(IST)
    res: Dict[str, Any] = {"ok": False, "dry_run": dry_run, "symbols": 0, "baselines": 0, "alerts": [], "sent": False, "message": None, "error": None}
    if not _LOCK.acquire(blocking=False):
        res["error"] = "already running"
        return res
    try:
        _LAST.update(status="running", error=None)
        syms = fetch_watchlist()[:max_symbols()]
        res["symbols"] = len(syms)
        prices: Dict[str, Any] = {}
        for s in syms:
            prices[s] = fetch_price(s)
            time.sleep(0.1)
        old = load_state()
        new_state, alerts = evaluate(old, syms, prices, now, threshold_pct())
        res["baselines"] = len(new_state)
        res["alerts"] = alerts
        msg = format_alert(alerts)
        res["message"] = msg
        if dry_run:
            res["ok"] = True
            return res
        if msg and not send(msg["title"], msg["message"]):
            res["error"] = "notification service did not accept the message (will retry on the next check)"
            keep = {a["symbol"] for a in alerts}                # not marked alerted: the alert is retried; baselines and re-arms are still saved
            for s in keep:
                if s in new_state and s in old and isinstance(old[s], dict):
                    new_state[s]["alerted"] = old[s].get("alerted")
        else:
            res["sent"] = bool(msg)
            for a in alerts:
                if a["symbol"] in new_state:
                    new_state[a["symbol"]]["alerted"] = a["side"]
        save_state(new_state)
        res["ok"] = res["error"] is None
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        logger.warning("watchlist_moves: run failed: %s", res["error"])
    finally:
        _LAST.update(status="idle" if res["error"] is None else "error", symbols=res["symbols"], alerts=len(res["alerts"]), sent=len(res["alerts"]) if res["sent"] else 0,
                     error=res["error"], at=datetime.now(IST).isoformat(timespec="seconds"))
        if _LOCK.locked():
            _LOCK.release()
    return res


def status() -> Dict[str, Any]:
    return {"enabled": enabled(), "threshold_pct": threshold_pct(), "check_minutes": check_minutes(), "max_symbols": max_symbols(),
            "market_hours_ist": f"{OPEN_HHMM}-{CLOSE_HHMM}", "baselines": len(load_state()), "last": dict(_LAST), "alert_only": True}


async def loop() -> None:
    import asyncio
    await asyncio.sleep(120)
    while True:
        try:
            now = datetime.now(IST)
            if enabled() and market_open(now):
                await asyncio.get_event_loop().run_in_executor(None, run_once)
        except Exception as e:  # noqa: BLE001
            logger.warning("watchlist_moves: loop error: %s", e)
        await asyncio.sleep(check_minutes() * 60)
