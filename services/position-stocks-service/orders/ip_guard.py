"""orders/ip_guard.py - E285 round 95: Dhan's static-IP rule, handled in one place (Position Stocks).

WHY: Dhan accepts order placement, modification and cancellation (including a Super Order leg cancel) only from an IP registered in the Dhan console (one primary, one secondary; the registration can be
changed once in 7 days). A request from any other IP is refused (Dhan's own FAQ shows the text as "DH-905 Invalid IP"). Reads - funds, positions, the order book - are NOT IP-gated, so the token check keeps saying
"ok" while every order fails. Until now this service had no handling for it: each candidate's BUY failed the same way, every cycle, all day, one order-budget slot each.

WHAT (every Dhan failure already passes through execution/dhan_client._extract_data, which calls on_rejection below):
  * a refusal that reads as an IP rejection pauses NEW entries for ENTRY_IP_PAUSE_MINUTES (orders/entry_pause.pause_all) and sends one alert per IP_ALERT_EVERY_MIN minutes.
  * exits, stops, square-off and reconcile are never paused: they must keep trying (the IP may be fixed in the next minute).
  * summarize_ip_status() turns Dhan's GET /v2/ip/getIP answer (primaryIP, secondaryIP, detectedIP, ipMatchStatus, ordersAllowed, modifyDatePrimary, modifyDateSecondary) into one verdict for GET /dhan/ip-status.
    That answer's field names come from Dhan's community post and two unofficial clients, not from a page of Dhan's docs; every field is optional here and an unknown shape reads as UNKNOWN, never as OK.
Pure apart from the pause and the alert; never raises. Off with IP_GUARD_ENABLED=false.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime
from typing import Any, Dict, Optional

import config

logger = logging.getLogger("position-stocks-ip-guard")

# Text a Dhan refusal for a wrong source IP carries. "dh-905" alone is NOT a marker: Dhan uses that code for other input errors too.
IP_MARKERS = ("invalid ip", "ip not whitelisted", "not whitelisted", "ip address not allowed", "unauthorized ip", "ip mismatch", "ip is not registered", "static ip")
OK_STATUSES = ("PRIMARY_MATCH", "SECONDARY_MATCH")

_lock = threading.Lock()
_state: Dict[str, Any] = {"hits": 0, "first_at": None, "last_at": None, "last_msg": "", "last_alert": None}


def is_ip_rejection(message: Any) -> bool:
    try:
        m = str(message or "").lower()
        return any(k in m for k in IP_MARKERS)
    except Exception:  # noqa: BLE001
        return False


def _now() -> float:
    return time.time()


def reset() -> None:
    with _lock:
        _state.update({"hits": 0, "first_at": None, "last_at": None, "last_msg": "", "last_alert": None})


def on_rejection(message: Any, now: Optional[float] = None) -> bool:
    """Called with the text of any Dhan failure. Returns True when it was an IP rejection (the pause is set, the alert sent when due). Never raises."""
    try:
        if not bool(getattr(config, "IP_GUARD_ENABLED", True)) or not is_ip_rejection(message):
            return False
        t = _now() if now is None else float(now)
        msg = str(message)[:240]
        with _lock:
            _state["hits"] += 1
            _state["first_at"] = _state["first_at"] or t
            _state["last_at"], _state["last_msg"] = t, msg
            every = max(1, int(getattr(config, "IP_ALERT_EVERY_MIN", 30))) * 60.0
            alert = _state["last_alert"] is None or (t - float(_state["last_alert"])) >= every
            if alert:
                _state["last_alert"] = t
        try:
            from orders import entry_pause
            entry_pause.pause_all("INVALID_IP", float(getattr(config, "ENTRY_IP_PAUSE_MINUTES", 15)))
        except Exception:  # noqa: BLE001
            logger.debug("ip guard: could not set the entry pause", exc_info=True)
        logger.error("position-stocks: Dhan refused an order call for its source IP: %s", msg)
        if alert:
            try:
                import notifier
                notifier.notify_critical(
                    "🚨 <b>Dhan refused an order: IP not registered</b>\n"
                    f"{msg}\n"
                    f"New entries are paused for {int(getattr(config, 'ENTRY_IP_PAUSE_MINUTES', 15))} min (exits and stops keep working). "
                    "Open GET /dhan/ip-status to see the IP Dhan detected and the IPs it has on file, then fix it in Dhan Web (Profile > Get Trading & Data APIs > Add IP). "
                    "Dhan allows one IP change in 7 days, so check before you change it.")
            except Exception:  # noqa: BLE001
                logger.debug("ip guard: alert failed", exc_info=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def status() -> Dict[str, Any]:
    with _lock:
        return {"enabled": bool(getattr(config, "IP_GUARD_ENABLED", True)), "hits": int(_state["hits"]), "first_at": _state["first_at"], "last_at": _state["last_at"],
                "last_message": _state["last_msg"], "pause_minutes": int(getattr(config, "ENTRY_IP_PAUSE_MINUTES", 15))}


def _text(v: Any) -> Optional[str]:
    s = str(v).strip() if v is not None else ""
    return s or None


def _parse_day(v: Any) -> Optional[date]:
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except Exception:  # noqa: BLE001
        return None


def summarize_ip_status(data: Any, outbound_ip: Optional[str] = None, today: Optional[date] = None) -> Dict[str, Any]:
    """One verdict from Dhan's getIP answer. verdict: OK | MISMATCH | NOT_CONFIGURED | ORDERS_NOT_ALLOWED | UNKNOWN.
    outbound_ip is what an IP-echo service says this host uses; Dhan's own detectedIP wins when they differ. A change can be made when its modifyDate is today or earlier."""
    out: Dict[str, Any] = {"verdict": "UNKNOWN", "ok": False, "ip_match_status": None, "detected_ip": None, "primary_ip": None, "secondary_ip": None, "orders_allowed": None,
                           "outbound_ip": _text(outbound_ip), "primary_change_allowed": None, "secondary_change_allowed": None, "advice": ""}
    try:
        if not isinstance(data, dict):
            out["advice"] = "Dhan's answer was not a JSON object; nothing could be read."
            return out
        t = today or date.today()
        status_txt = (_text(data.get("ipMatchStatus")) or "").upper() or None
        out.update({"ip_match_status": status_txt, "detected_ip": _text(data.get("detectedIP")), "primary_ip": _text(data.get("primaryIP")), "secondary_ip": _text(data.get("secondaryIP")),
                    "modify_date_primary": _text(data.get("modifyDatePrimary")), "modify_date_secondary": _text(data.get("modifyDateSecondary"))})
        oa = data.get("ordersAllowed")
        out["orders_allowed"] = oa if isinstance(oa, bool) else None
        for key, fld in (("primary_change_allowed", "modifyDatePrimary"), ("secondary_change_allowed", "modifyDateSecondary")):
            d = _parse_day(data.get(fld))
            out[key] = (d <= t) if d else None
        if status_txt in OK_STATUSES and oa is not False:
            out.update({"verdict": "OK", "ok": True, "advice": f"Dhan matched this host's IP ({out['detected_ip'] or 'detected'}) to your {status_txt.split('_')[0].lower()} IP. Orders are accepted from here."})
        elif status_txt in OK_STATUSES and oa is False:
            out.update({"verdict": "ORDERS_NOT_ALLOWED", "advice": "The IP matches but Dhan says orders are not allowed for this account; ask Dhan support."})
        elif status_txt == "NOT_CONFIGURED":
            out.update({"verdict": "NOT_CONFIGURED", "advice": "No static IP is registered. Add this host's IP in Dhan Web (Profile > Get Trading & Data APIs > Add IP); every order is refused until then."})
        elif status_txt == "MISMATCH":
            can = out["primary_change_allowed"] if out["primary_ip"] else out["secondary_change_allowed"]
            when = f" A change is allowed now." if can else (f" The next change is allowed on {out['modify_date_primary'] or out['modify_date_secondary']}." if (out["modify_date_primary"] or out["modify_date_secondary"]) else "")
            out.update({"verdict": "MISMATCH", "advice": f"Dhan sees {out['detected_ip'] or 'a different IP'} but has {out['primary_ip'] or '-'} (primary) and {out['secondary_ip'] or '-'} (secondary) on file. Orders are refused from this host.{when}"})
        else:
            out["advice"] = "Dhan's answer had no ipMatchStatus I recognise; check the raw answer."
        if out["outbound_ip"] and out["detected_ip"] and out["outbound_ip"] != out["detected_ip"]:
            out["advice"] += f" (An IP-echo service says this host uses {out['outbound_ip']}, Dhan detected {out['detected_ip']}: go by Dhan's.)"
        return out
    except Exception:  # noqa: BLE001
        out["verdict"] = "UNKNOWN"
        return out
