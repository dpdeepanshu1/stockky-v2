"""orders/or_live.py - round 612: LIVE "OR" rescue for the Position Stocks `WINDOW_NO_SETUP` refusal. Pure apart from a small shared per-day counter file; never raises (fails CLOSED: any doubt = no rescue).

Why: every entry gate is an AND, so a name with no matched setup waits whatever else is true. Rule B: price above VWAP (must-have) AND at least OR_LIVE_MIN_CONFIRMS (default 2) of
three other confirmations: a typed POSITIVE event on the name, its sector in the top third, the order-flow tape leaning up. A missing fact is not true. HARD safety gates (kill switch,
loss limits, order budget, stop-out guard, entry cap, held symbol, price guard, depth, circuit, capital) are untouched and still run after a rescue.

Safety: only from OR_LIVE_SINCE_IST (default 09:45); at most OR_LIVE_MAX_PER_DAY rescues per IST day, ONE count for both services and kept across restarts (round 613; default 4; 0 = none); OR_LIVE_MODE=off turns it off (default live).
A rescue is logged (`OR_LIVE[...]`) and stamped in flow_json["or_live"].
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, Optional

import config
from orders import shared_or_budget as _sb

KEY = "or_live"
SERVICE = "ps"                         # tag in the shared budget file
_LAST: Dict[str, Any] = {"day": None, "n": 0, "source": None}   # what this process saw at its last take
_PENDING: Dict[Any, Any] = {}            # round 614: key -> (day, source) of a rescue taken whose entry has not been settled yet
_PLOCK = threading.Lock()


def mode() -> str:
    return str(getattr(config, "OR_LIVE_MODE", "live") or "live").strip().lower()


def enabled() -> bool:
    return mode() == "live"


def time_ok(now: Any) -> bool:
    try:
        hm = "%02d:%02d" % (now.hour, now.minute)
        since = str(getattr(config, "OR_LIVE_SINCE_IST", "09:45") or "09:45").strip()
        return len(since) == 5 and hm >= since
    except Exception:  # noqa: BLE001
        return False


def rule_b(window_reason: Any, event_polarity: Any, above_vwap: Any, sector_third: Any, tape_sign: Any = None) -> Optional[Dict[str, Any]]:
    try:
        if not window_reason or above_vwap is not True:
            return None
        got = []
        if event_polarity == 1:
            got.append("event")
        if sector_third == "top":
            got.append("sector_top")
        if isinstance(tape_sign, (int, float)) and not isinstance(tape_sign, bool) and tape_sign > 0:
            got.append("order_flow")
        if len(got) < max(1, int(getattr(config, "OR_LIVE_MIN_CONFIRMS", 2))):
            return None
        return {"rule": "B", "confirms": ["vwap"] + got, "refused": str(window_reason)[:160]}
    except Exception:  # noqa: BLE001
        return None


def take(day: Any) -> bool:
    """Use one of today's rescues. False when the day's budget is spent. `day` is the IST date. Round 613: the budget is ONE count shared by both services and kept across restarts
    (shared_or_budget.py); if its file cannot be used the count is per process, as in round 612."""
    try:
        cap = int(getattr(config, "OR_LIVE_MAX_PER_DAY", 4))
        ok, n, src = _sb.take(day, cap, SERVICE)
        _LAST["day"], _LAST["n"], _LAST["source"] = str(day), int(n), src
        return bool(ok)
    except Exception:  # noqa: BLE001
        return False


def reset() -> None:
    """Tests: forget this process's count and delete the shared file."""
    _sb.reset_process()
    _LAST["day"], _LAST["n"], _LAST["source"] = None, 0, None
    with _PLOCK:
        _PENDING.clear()
    for p in (_sb.path(), _sb.path() + ".lock"):
        try:
            os.remove(p)
        except OSError:
            pass


def used_today() -> int:
    """Rescues used today as last seen by this process (the shared total when the file works)."""
    return int(_LAST["n"])


def budget_source() -> Optional[str]:
    """"file" (shared, kept across restarts) or "process" (the file could not be used) at the last take; None before any."""
    return _LAST["source"]


def _note_pending(key: Any, day: Any) -> None:
    if key is None:
        return
    with _PLOCK:
        _PENDING[key] = (str(day), _LAST.get("source") or "file")


def allow(hit: Optional[Dict[str, Any]], now: Any, key: Any = None) -> bool:
    """True when `hit` may let the refused entry through: live mode, time ok, day budget left. `key` (round 614) names the entry so its rescue can be given back if no order follows."""
    try:
        if not hit or not enabled() or not time_ok(now):
            return False
        d = now.date()
        if not take(d):
            return False
        _note_pending(key, d)
        return True
    except Exception:  # noqa: BLE001
        return False


def refund_enabled() -> bool:
    """Round 614: OR_LIVE_REFUND=0 keeps a rescue spent even when its entry never became an order (round 613 behaviour)."""
    return (os.getenv("OR_LIVE_REFUND") or "1").strip().lower() not in ("0", "false", "off", "no")


def give_back(key: Any) -> bool:
    """Round 614: the rescue taken for `key` (see allow) did not end in an order: return it to the day's budget. Fails SAFE: any doubt = it stays spent. Never raises."""
    try:
        with _PLOCK:
            got = _PENDING.pop(key, None)
        if not got or not refund_enabled():
            return False
        ok = bool(_sb.give_back(got[0], SERVICE, got[1]))
        if ok and _LAST["n"] > 0:
            _LAST["n"] -= 1
        return ok
    except Exception:  # noqa: BLE001
        return False


def keep(key: Any) -> None:
    """Round 614: the rescue for `key` ended in an order (or cannot be told): it stays spent."""
    with _PLOCK:
        _PENDING.pop(key, None)


def pending_keys() -> list:
    with _PLOCK:
        return list(_PENDING.keys())


def stamp(flow_json: Any, hit: Optional[Dict[str, Any]]) -> Any:
    try:
        if not isinstance(hit, dict):
            return flow_json
        if isinstance(flow_json, str) and flow_json.strip():
            d = json.loads(flow_json)
        elif isinstance(flow_json, dict):
            d = dict(flow_json)
        else:
            d = {}
        if not isinstance(d, dict):
            return flow_json
        lst = d.get(KEY)
        lst = list(lst) if isinstance(lst, list) else []
        lst.append(hit)
        d[KEY] = lst[-4:]
        return json.dumps(d, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001
        return flow_json
