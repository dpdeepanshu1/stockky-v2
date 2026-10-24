"""weather_apply.py - Market Weather LIVE apply (Part 1 step 6). The ONLY module that can send a dial; weather.py cannot (a test pins that).

Three independent locks must ALL be open before one dial moves, and any of them closed leaves behaviour exactly as today:
  1. the runner switch  WEATHER_APPLY / PUT /weather/settings {apply:true}   (off by default)
  2. the evidence gate  WEATHER_GATE_MODE: replay_only (default) = a regime_history_replay verdict USEFUL (<= 45 days old), nothing else;
                        strict = that AND a regime_shadow_report verdict PASS (<= 14 days old); off = no evidence needed (the dials stay tighten-only)
  3. each trading service's own MARKET_STATE_MODE=live (a protected switch only YOU set); the service refuses any entry that is not a tightening
     of its SAVED value, refuses non-overlay dials, and expires everything at 15:30 IST. The runner also clamps locally (profiles.build_overlay),
     so one loose dial can never make a service reject the whole all-or-nothing call.
Failure is always the safe way round: an unreachable service, a refused call or a failed gate means no dial; an overlay that was set stays until 15:30.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

from regime import profiles

REPLAY_MAX_AGE_DAYS = 45
SHADOW_MAX_AGE_DAYS = 14
_REPLAY_OK = re.compile(r"^VERDICT:\s*USEFUL\s*$", re.M)
_SHADOW_OK = re.compile(r"^REGIME_SHADOW:\s*PASS\s*$", re.M)


GATE_MODES = ("replay_only", "strict", "off")
DEFAULT_GATE_MODE = "replay_only"


def norm_gate_mode(v: Any) -> str:
    """An unknown value falls back to the STRICTEST mode, never a looser one."""
    v = str(v or "").strip().lower()
    return v if v in GATE_MODES else ("strict" if v else DEFAULT_GATE_MODE)


def evidence_gate(evidence_for: Callable[[str, float], Any], mode: str = "strict") -> Tuple[bool, str]:
    """evidence_for(job_id, max_age_seconds) -> an object with .text, or None.
    strict: replay USEFUL and shadow report PASS (both fresh). replay_only: replay USEFUL (fresh) only. off: always open. NO_EDGE / NOT_ENOUGH_DATA block in both non-off modes."""
    mode = norm_gate_mode(mode)
    if mode == "off":
        return True, "gate off (WEATHER_GATE_MODE=off): no evidence required, dials are still tighten-only"
    rep = evidence_for("regime_history_replay", REPLAY_MAX_AGE_DAYS * 86400.0)
    if rep is None:
        return False, f"no regime_history_replay result in the last {REPLAY_MAX_AGE_DAYS} days"
    if not _REPLAY_OK.search(getattr(rep, "text", "") or ""):
        return False, "the latest regime_history_replay verdict is not USEFUL"
    if mode == "replay_only":
        return True, "replay USEFUL (shadow report not required: WEATHER_GATE_MODE=replay_only)"
    sh = evidence_for("regime_shadow_report", SHADOW_MAX_AGE_DAYS * 86400.0)
    if sh is None:
        return False, f"no regime_shadow_report result in the last {SHADOW_MAX_AGE_DAYS} days"
    if not _SHADOW_OK.search(getattr(sh, "text", "") or ""):
        return False, "the latest regime_shadow_report verdict is not PASS"
    return True, "replay USEFUL and shadow report PASS"


def ensure_replay(evidence_for: Callable[[str, float], Any], submit: Callable[..., Any], mode: str) -> str:
    """Start-up helper: when the gate needs a replay and none is fresh, queue regime_history_replay ourselves. Returns a short note; never raises."""
    try:
        if norm_gate_mode(mode) == "off":
            return "gate off: no replay needed"
        if evidence_for("regime_history_replay", REPLAY_MAX_AGE_DAYS * 86400.0) is not None:
            return "a fresh replay result exists"
        submit("job", "regime_history_replay", {}, by="weather-startup", trigger="startup")
        return "no fresh replay result: regime_history_replay queued"
    except Exception as e:  # noqa: BLE001 - already queued, queue full, runner not ready: the page's Run button still works
        return f"replay not queued: {type(e).__name__}: {e}"[:200]


class Applier:
    """apply(state, now) -> {"applied": bool, "why": str, "services": {svc: {...}}}. Never raises."""

    def __init__(self, control: Any, gate: Callable[[], Tuple[bool, str]], rt_key: str = "rt", ps_key: str = "ps", mode_fn: Optional[Callable[[], str]] = None):
        self.control, self.gate, self.mode_fn = control, gate, mode_fn
        self.keys = {"real-trade": rt_key, "position-stocks": ps_key}

    def gate_status(self) -> Dict[str, Any]:
        """What the Weather card shows: the mode and the verdict that opened or closed the gate. Never raises."""
        try:
            ok, why = self.gate()
        except Exception as e:  # noqa: BLE001
            ok, why = False, f"gate error: {type(e).__name__}"
        return {"mode": norm_gate_mode(self.mode_fn()) if self.mode_fn else None, "open": bool(ok), "why": why}

    def __call__(self, state: str, now: datetime) -> Dict[str, Any]:
        out: Dict[str, Any] = {"applied": False, "state": state, "why": "", "services": {}}
        try:
            ok, why = self.gate()
        except Exception as e:  # noqa: BLE001
            ok, why = False, f"gate error: {type(e).__name__}"
        if not ok:
            out["why"] = f"not applied: {why}"
            return out
        any_ok = False
        for svc in profiles.SERVICES:
            r: Dict[str, Any] = {"ok": False}
            out["services"][svc] = r
            key = self.keys[svc]
            try:
                rows = self.control.flags(key)
                mode = (rows.get("MARKET_STATE_MODE") or {}).get("desired")
                if mode != "live":
                    r["why"] = f"service MARKET_STATE_MODE is {mode or 'unknown'}, not live: nothing sent"
                    continue
                saved = {k: (rows.get(k) or {}).get("desired") for k in profiles.DIALS}
                body = profiles.build_overlay(svc, state, saved, now)
                if body["until"] is None:
                    r["why"] = "past 15:30 IST: nothing to apply"
                    continue
                r["skipped"] = body["skipped"]
                r["entries"] = body["entries"]
                self.control.overlay(key, {"entries": body["entries"], "until": body["until"], "state": body["state"], "version": body["version"],
                                           "note": "market weather (live)"})
                r["ok"] = True
                any_ok = True
            except Exception as e:  # noqa: BLE001 - a refused / unreachable service just gets no dial
                r["why"] = f"{type(e).__name__}: {e}"[:200]
        out["applied"] = any_ok
        out["why"] = "applied" if any_ok else "no service accepted a dial"
        return out
