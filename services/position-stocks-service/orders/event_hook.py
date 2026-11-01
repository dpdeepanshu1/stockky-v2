"""orders/event_hook.py - a fresh typed event AGAINST an open trade pulls its stop in (E272 round 11, 31 Oct 2026).

One pass per fast-reconcile cycle (main.py), right after the tape hook. For every OPEN Super Order position (not an overnight CNC carry) the typed event feed
(analysis-intelligence GET /events/{symbol}/active) is read. When the strongest known-polarity event is AGAINST the trade (negative on a long, positive on a
short), was announced AFTER the trade opened (so it was not known at entry) and has at least EVENT_HELD_MIN_STRENGTH, the stop is moved to keep only
(1 - EVENT_HELD_TIGHTEN_FRAC) of its current distance behind the live price. `decide` is pure; this file reads, applies and stamps.
  * a stop is never loosened; a stop on the wrong side of the price is the stop watchdog's case (no move);
  * a plain short (no Super Order) is never touched: its broker stop is fixed by design (E114);
  * each event (announced_at) tightens a trade once: stamped in flow_json["event_tighten"];
  * the stop leg is modified through dhan_client.modify_super_order and read back with verify_stop_leg; an unconfirmed modify leaves the stored value alone;
  * feed down / unknown polarity / no event = nothing happens. It tightens only; it never exits a trade by itself (exits on 3+ momentum signals are E275).
Per-symbol feed answers are cached EVENT_HELD_CACHE_S seconds. Never raises.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote

logger = logging.getLogger("position-stocks-event-hook")

_cache: Dict[str, tuple] = {}
_stats: Dict[str, int] = {"passes": 0, "checked": 0, "tightened": 0, "unconfirmed": 0, "errors": 0}


def _cfg(name: str, default):
    try:
        import config
        return getattr(config, name, default)
    except Exception:  # noqa: BLE001
        return default


def _utc(raw) -> Optional[datetime]:
    try:
        if raw is None or raw == "":
            return None
        if isinstance(raw, datetime):
            d = raw
        else:
            d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def strongest_event(body) -> Optional[dict]:
    """Strongest event with a KNOWN polarity in a /events/{symbol}/active body: {type, polarity, strength, announced_at}. None otherwise. Never raises."""
    try:
        if not isinstance(body, dict) or not body.get("ok"):
            return None
        best = None
        for e in body.get("events") or []:
            if not isinstance(e, dict):
                continue
            pol = int(float(e.get("polarity") or 0))
            if pol not in (1, -1):
                continue
            st = float(e.get("strength") or 0.0)
            if best is None or st > best["strength"]:
                best = {"type": str(e.get("type") or "other"), "polarity": pol, "strength": st, "announced_at": e.get("published")}
        return best
    except Exception:  # noqa: BLE001
        return None


def decide(*, side: str, entry_ts, stop: float, ltp: float, tick: float, event: Optional[dict], already_seen: Optional[str] = None,
           min_strength: float = 0.4, tighten_frac: float = 0.5) -> Dict[str, Any]:
    """Pure. Returns {"action": "HOLD"|"TIGHTEN", "reason": str, "new_stop": float|None}."""
    hold = lambda why: {"action": "HOLD", "reason": why, "new_stop": None}  # noqa: E731
    try:
        if not event:
            return hold("no known-polarity event")
        short = str(side).upper() == "SHORT"
        against = -1 if not short else 1                       # polarity that hurts this side
        if int(event.get("polarity") or 0) != against:
            return hold("event is not against the trade")
        if float(event.get("strength") or 0.0) < float(min_strength):
            return hold("event too weak")
        ann = _utc(event.get("announced_at"))
        opened = _utc(entry_ts)
        if ann is None or opened is None:
            return hold("announce or open time unknown")       # cannot prove it was news after entry
        if ann <= opened:
            return hold("event was known at entry")
        if already_seen and str(event.get("announced_at")) == str(already_seen):
            return hold("this event already tightened the trade")
        stop, ltp, tick = float(stop), float(ltp), max(float(tick or 0.0), 0.0)
        s = -1.0 if short else 1.0                              # long: stop below price
        dist = s * (ltp - stop)
        if dist <= 0:
            return hold("stop on the wrong side of the price (watchdog case)")
        frac = max(0.0, min(0.95, float(tighten_frac)))
        new = ltp - s * dist * (1.0 - frac)
        if tick > 0:
            new = round(round(new / tick) * tick, 2)           # nearest tick; the checks below refuse a result that loosens or crosses the price
        new = round(new, 2)
        if s * (new - stop) <= 0:
            return hold("no tighter than the current stop")
        if s * (ltp - new) <= 0:
            return hold("new stop would sit at or past the price")
        return {"action": "TIGHTEN", "reason": f"{event.get('type', 'event')} against the trade (strength {float(event.get('strength') or 0):.2f})",
                "new_stop": new}
    except Exception as e:  # noqa: BLE001
        return hold(f"decide error: {e}")


def _fetch(symbol: str) -> Optional[dict]:
    """Cached sync read of the symbol's body from the event feed. None on any failure."""
    ttl = float(_cfg("EVENT_HELD_CACHE_S", 60.0))
    hit = _cache.get(symbol)
    now = time.monotonic()
    if hit and now - hit[0] < ttl:
        return hit[1]
    body = None
    try:
        import httpx
        r = httpx.get(f"{_cfg('EVENT_URL', '')}/events/{quote(symbol, safe='&')}/active", timeout=float(_cfg("QUALITY_GATE_TIMEOUT_S", 3.0)))
        body = r.json() if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        body = None
    if len(_cache) > 500:
        _cache.clear()
    _cache[symbol] = (now, body)
    return body


def _stamp(flow_json, rec: dict):
    try:
        d = json.loads(flow_json) if isinstance(flow_json, str) and flow_json else (dict(flow_json) if isinstance(flow_json, dict) else {})
        if not isinstance(d, dict):
            d = {}
        lst = d.get("event_tighten")
        lst = list(lst) if isinstance(lst, list) else []
        lst.append(rec)
        d["event_tighten"] = lst[-10:]
        return json.dumps(d, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001
        return flow_json


def _seen(flow_json) -> Optional[str]:
    try:
        d = json.loads(flow_json) if isinstance(flow_json, str) and flow_json else (flow_json if isinstance(flow_json, dict) else {})
        lst = (d or {}).get("event_tighten") or []
        return str(lst[-1].get("event_at")) if lst else None
    except Exception:  # noqa: BLE001
        return None


def stats() -> dict:
    return dict(_stats)


def _reset_for_tests() -> None:
    _cache.clear()
    for k in _stats:
        _stats[k] = 0


def run_event_tighten(db, *, fetch: Optional[Callable[[str], Optional[dict]]] = None, ltp_fn: Optional[Callable[[str], Optional[float]]] = None) -> int:
    """Returns the number of positions whose stop was moved this pass."""
    if not _cfg("EVENT_HELD_TIGHTEN_ENABLED", True):
        return 0
    _stats["passes"] += 1
    from execution import dhan_client
    from models import ScalpPosition
    from orders import side as side_mod
    if fetch is None:
        fetch = _fetch
    if ltp_fn is None:
        def ltp_fn(sym):  # noqa: E306
            try:
                from feed import ws_client
                buf = ws_client.get_tick_buffer(sym)
                v = buf[-1][1] if buf else None
                return float(v) if v and v > 0 else None
            except Exception:  # noqa: BLE001
                return None
    moved = 0
    try:
        positions = db.query(ScalpPosition).filter(ScalpPosition.status == "OPEN").all()
    except Exception as e:  # noqa: BLE001
        logger.warning("event-tighten: could not load positions: %s", e)
        return 0
    for pos in positions:
        try:
            if getattr(pos, "overnight_converted_to_cnc", False) or not getattr(pos, "dhan_super_order_id", None):
                continue                                        # CNC carry, or a plain short (E114: broker stop fixed)
            ltp = ltp_fn(pos.symbol)
            if ltp is None:
                continue
            _stats["checked"] += 1
            ev = strongest_event(fetch(pos.symbol))
            side = "SHORT" if side_mod.is_short(pos) else "LONG"
            d = decide(side=side, entry_ts=getattr(pos, "opened_at", None), stop=pos.stop_price, ltp=ltp, tick=dhan_client.tick_size_for_price(ltp),
                       event=ev, already_seen=_seen(getattr(pos, "flow_json", None)),
                       min_strength=float(_cfg("EVENT_HELD_MIN_STRENGTH", 0.4)), tighten_frac=float(_cfg("EVENT_HELD_TIGHTEN_FRAC", 0.5)))
            if d["action"] != "TIGHTEN":
                continue
            new_stop, old_stop = float(d["new_stop"]), pos.stop_price
            try:
                dhan_client.modify_super_order(db, order_id=pos.dhan_super_order_id, order_leg="STOP_LOSS_LEG", stop_loss_price=new_stop)
                if dhan_client.verify_stop_leg(db, order_id=pos.dhan_super_order_id, expected_price=new_stop) is False:
                    _stats["unconfirmed"] += 1
                    logger.warning("EVENT_TIGHTEN: %s (id=%d) stop modify to %.2f NOT confirmed by Dhan - stored stop left at %s", pos.symbol, pos.id, new_stop, old_stop)
                    continue
            except Exception as e:  # noqa: BLE001
                _stats["errors"] += 1
                logger.warning("EVENT_TIGHTEN: %s (id=%d) stop modify to %.2f rejected (%s) - leg unchanged", pos.symbol, pos.id, new_stop, e)
                continue
            pos.stop_price = new_stop
            pos.stop_moved_to_breakeven = True                  # breakeven.py can never lower it again
            pos.flow_json = _stamp(getattr(pos, "flow_json", None), {
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event_at": ev.get("announced_at"), "type": ev.get("type"),
                "strength": round(float(ev.get("strength") or 0), 3), "ltp": round(float(ltp), 2), "old_stop": old_stop, "new_stop": new_stop})
            db.commit()
            _stats["tightened"] += 1
            moved += 1
            logger.warning("EVENT_TIGHTEN: %s (id=%d) %s -> stop %s -> %s (ltp %.2f)", pos.symbol, pos.id, d["reason"], old_stop, new_stop, ltp)
        except Exception as e:  # noqa: BLE001
            _stats["errors"] += 1
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.warning("event-tighten: %s failed: %s", getattr(pos, "symbol", "?"), e)
    return moved
