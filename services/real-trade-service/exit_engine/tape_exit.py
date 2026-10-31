"""tape_exit.py - pure decision: should a reliable ADVERSE order-flow tape pull an open trade's stop in / trim its target (E270, 30 Oct 2026).

Byte-identical in real-trade-service/exit_engine/ and position-stocks-service/orders/ (scripts/check_shared_copies.py, IDENTICAL family). Stdlib only, no I/O,
no config: the caller passes every number, so the same maths runs in both services and in the tests.

RULES (LONG and SHORT are mirrored with one sign s: +1 long, -1 short; "better" means s * price is higher):
  * the tape must be RELIABLE and have at least `min_against` adverse checks, and at least one must be `delta` or `imbalance`
    (absorption / exhaustion / book lean): the timing checks alone cannot say the move is over;
  * the STOP is tightened only when the trade is already `min_profit_pct` in profit (zone "profit"), or the price has used up `near_stop_frac` of the way
    from entry to the current stop (zone "near_stop"). The new stop sits `gap_pct` behind the live price, rounded toward the price (a long's level at or
    below it, a short's at or above it), moves only in the protective direction by at least two ticks and never past the target;
  * the TARGET is only ever trimmed NEARER: one tick before the next HVN wall on the live-price side (`hvn_above` for a long, `hvn_below` for a short),
    and only when it still pays `min_target_pct` from entry and stays beyond the live price. A target is never raised and a stop is never loosened;
  * unreliable / missing tape, a bad number or any error = HOLD (no opinion). It can only make an exit sooner, never later, and never raises.
"""
from __future__ import annotations

import math
from typing import Optional

_NEEDS = ("delta", "imbalance")


def _num(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def _floor(x: float, tick: float) -> float:
    return round(math.floor(x / tick + 1e-9) * tick, 4)


def _ceil(x: float, tick: float) -> float:
    return round(math.ceil(x / tick - 1e-9) * tick, 4)


def _toward_price(level: float, s: int, tick: float) -> float:
    """A LONG's stop lands at or BELOW the live price, a SHORT's at or ABOVE it."""
    return _floor(level, tick) if s > 0 else _ceil(level, tick)


def _hold(reason: str, against: int = 0, zone: Optional[str] = None) -> dict:
    return {"action": "HOLD", "new_stop": None, "new_target": None, "reason": reason, "against": against, "zone": zone}


def decide(*, side: str, entry, stop, target, ltp, tape, tick,
           min_against: int = 3, min_profit_pct: float = 0.3, near_stop_frac: float = 0.5,
           gap_pct: float = 0.25, min_target_pct: float = 0.5) -> dict:
    """{"action": "HOLD"|"TIGHTEN", "new_stop", "new_target", "reason", "against", "zone"}. Never raises."""
    try:
        s = -1 if str(side or "").strip().upper() in ("SHORT", "SELL") else 1
        entry, ltp, tick = _num(entry), _num(ltp), _num(tick)
        stop, target = _num(stop), _num(target)
        if entry is None or ltp is None or tick is None:
            return _hold("bad_number")
        if not isinstance(tape, dict) or not tape.get("available", True):
            return _hold("no_tape")
        if not tape.get("reliable"):
            return _hold("tape_unreliable")
        checks = tape.get("checks") or {}
        adverse = [k for k, v in checks.items() if v == "against"]
        against = len(adverse)
        if against < int(min_against):
            return _hold("not_enough_against", against)
        if not any(k in adverse for k in _NEEDS):
            return _hold("needs_delta_or_imbalance", against)

        profit_pct = s * (ltp / entry - 1.0) * 100.0
        zone = None
        new_stop = None
        if stop is not None:
            # stop must sit on the protective side of the live price, else the position is already past it: the stop watchdog owns that case
            if s * (ltp - stop) <= 0:
                return _hold("past_stop", against)
            used = s * (ltp - entry) / (s * (entry - stop)) if s * (entry - stop) > 0 else None
            # near_stop: price moved AGAINST the trade and used up near_stop_frac of the entry->stop room
            near = used is not None and (-used) >= float(near_stop_frac)
            if profit_pct >= float(min_profit_pct):
                zone = "profit"
            elif near:
                zone = "near_stop"
            if zone:
                cand = _toward_price(ltp * (1.0 - s * float(gap_pct) / 100.0), s, tick)
                better = s * (cand - stop) >= 2 * tick - 1e-9
                inside = s * (ltp - cand) > 0
                ok_target = target is None or s * (target - cand) > 0
                if better and inside and ok_target:
                    new_stop = cand

        new_target = None
        wall = _num(tape.get("hvn_above") if s > 0 else tape.get("hvn_below"))
        if wall is not None and target is not None and s * (wall - ltp) > 0:
            cand_t = round(wall - s * tick, 4)
            pays = s * (cand_t / entry - 1.0) * 100.0 >= float(min_target_pct)
            nearer = s * (target - cand_t) > 0
            beyond = s * (cand_t - ltp) > 0
            if pays and nearer and beyond:
                new_target = cand_t

        if new_stop is None and new_target is None:
            return _hold("nothing_to_tighten", against, zone)
        names = ",".join(adverse)
        return {"action": "TIGHTEN", "new_stop": new_stop, "new_target": new_target,
                "reason": f"TAPE_{zone or 'target'}:{against}_of_6_against({names})", "against": against, "zone": zone}
    except Exception:  # noqa: BLE001 - no opinion
        return _hold("error")
