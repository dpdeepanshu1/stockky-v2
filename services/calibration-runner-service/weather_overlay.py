"""E112: what each trading service is running TODAY against what it has SAVED, and a revert button (Market Weather card, open item 2).

Read side: `overlay_view` reads the flag list of each service's control API (the rows carry `desired` = the saved value and `overlay` = {value, saved, until, state}).
Write side: `revert` calls DELETE /control/overlay on ONE service, only when a person clicks the button. That puts every overlaid switch back to its saved value
and clears the state stamp. It does not stop the runner: a later 09:14 lock or a state change can send a tighter overlay again while the gate is open and
"Send dials" is on, so the card says so. This module cannot build a dial, never writes a saved value and never sets a switch.
Never raises: an unreachable service is reported as `ok: false` with a short reason.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from regime import profiles

# runner service name (profiles.SERVICES) -> the control client's service key
DEFAULT_KEYS = {"real-trade": "real-trade-service", "position-stocks": "position-stocks-service"}


def _row_view(key: str, row: Mapping[str, Any]) -> Dict[str, Any]:
    ovl = row.get("overlay") or None
    saved = row.get("desired")
    if ovl:
        today, until, state = ovl.get("value"), ovl.get("until"), ovl.get("state")
        saved = ovl.get("saved", saved)
    else:
        today, until, state = saved, None, None
    return {"key": key, "saved": saved, "today": today, "until": until, "state": state, "overlaid": bool(ovl)}


def service_view(control: Any, keys: Mapping[str, str], svc: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "mode": None, "dials": [], "overlaid": 0}
    try:
        rows = control.flags(keys[svc])
    except Exception as e:  # noqa: BLE001 - a service that is down must not break the card
        out["why"] = f"{type(e).__name__}: {e}"[:200]
        return out
    out["ok"] = True
    out["mode"] = (rows.get("MARKET_STATE_MODE") or {}).get("desired")
    for k, d in profiles.DIALS.items():
        if svc in d["services"] and k in rows:
            out["dials"].append(_row_view(k, rows[k]))
    out["overlaid"] = sum(1 for d in out["dials"] if d["overlaid"])
    return out


def overlay_view(control: Any, keys: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    keys = keys or DEFAULT_KEYS
    return {"services": {svc: service_view(control, keys, svc) for svc in profiles.SERVICES}}


def revert(control: Any, svc: str, keys: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Clear today's overlay on one service. ValueError for an unknown service name; any other failure is reported, not raised."""
    keys = keys or DEFAULT_KEYS
    if svc not in profiles.SERVICES:
        raise ValueError(f"unknown service {svc!r}: use one of {', '.join(profiles.SERVICES)}")
    res: Dict[str, Any] = {"service": svc, "ok": False, "cleared": []}
    try:
        r = control.overlay_clear(keys[svc]) or {}
        res["ok"] = bool(r.get("ok", True))
        res["cleared"] = list(r.get("cleared") or [])
    except Exception as e:  # noqa: BLE001
        res["why"] = f"{type(e).__name__}: {e}"[:200]
    res["view"] = service_view(control, keys, svc)
    return res
