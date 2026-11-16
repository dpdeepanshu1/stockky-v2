"""orders/penny_gate.py - skip a cheap-stock entry whose stop cannot survive one tick of slippage, and a dust-sized position (round 590, 2026-11-16).

WHY (16 Nov): AGL traded near Rs 14 as 4 shares, about Rs 57 of position, and was stopped out at -2.53 %. At that price one NSE tick (Rs 0.05) is 0.36 % of the
price, so a fill or a stop one or two ticks off the plan is already a large slice of a 2.5 % stop, and a Rs 57 position cannot earn anything that pays for the
fixed costs and the noise. A flat price floor (ENTRY_MIN_PRICE) would also drop cheap names whose stop is wide enough to absorb a tick, and the Stockky rule is
that a genuine liquid stock matters more than its price, so this is adaptive instead.

WHAT (both run after sizing, on the quantity that will really trade):
  1. tick friction: one tick as a % of the price, divided by the stop distance in %, must not exceed PENNY_TICK_MAX_STOP_FRAC (default 0.12). The more volatile the
     stock (wider stop), the cheaper a price it may have; a quiet cheap stock with a tight stop is refused. Refusal code PENNY_TICK.
  2. dust notional: qty x price below ENTRY_MIN_NOTIONAL_RS (default Rs 300, 0 = off) is refused. Refusal code MIN_NOTIONAL.

Pure functions, no DB, no network; any input problem lets the entry go on (never raises into the caller). Switches: PENNY_TICK_GATE_ENABLED, ENTRY_MIN_NOTIONAL_RS.
"""
from __future__ import annotations

import logging
from typing import Optional

import config

logger = logging.getLogger("position-stocks-penny-gate")


def _tick(price: float) -> float:
    from execution import dhan_client   # imported late: the module pulls the broker client in
    return float(dhan_client.tick_size_for_price(price))


def tick_friction(price: float, stop_pct: float, tick: Optional[float] = None) -> Optional[float]:
    """One tick as a fraction of the stop distance (0.14 = a single tick is 14 % of the risk). None when it cannot be worked out."""
    try:
        px, sp = float(price), float(stop_pct)
        if px <= 0 or sp <= 0:
            return None
        t = float(tick) if tick is not None else _tick(px)
        if t <= 0:
            return None
        return (t / px * 100.0) / sp
    except Exception:  # noqa: BLE001
        return None


def reject_reason(price: float, stop_pct: float, qty: int, *, tick: Optional[float] = None) -> Optional[str]:
    """None when the entry may go ahead (or the gate is off / cannot be evaluated), else the skip reason (starts with PENNY_TICK or MIN_NOTIONAL)."""
    try:
        px, q = float(price), int(qty)
        if px <= 0 or q <= 0:
            return None
        if bool(getattr(config, "PENNY_TICK_GATE_ENABLED", True)):
            cap = float(getattr(config, "PENNY_TICK_MAX_STOP_FRAC", 0.0) or 0.0)
            fr = tick_friction(px, float(stop_pct or 0.0), tick)
            if cap > 0 and fr is not None and fr > cap:
                t = float(tick) if tick is not None else _tick(px)
                return (f"PENNY_TICK:one tick Rs{t:.2f} is {fr * 100:.0f}% of the {float(stop_pct):.2f}% stop at ltp={px:.2f} "
                        f"(limit {cap * 100:.0f}%); a wider stop or a higher price is needed")
        floor = float(getattr(config, "ENTRY_MIN_NOTIONAL_RS", 0.0) or 0.0)
        if floor > 0 and px * q < floor:
            return f"MIN_NOTIONAL:qty={q} x ltp={px:.2f} = Rs{px * q:.2f} < Rs{floor:.0f}"
        return None
    except Exception as e:  # noqa: BLE001  a maths / config problem must never block or crash an entry
        logger.debug("penny gate: evaluate failed, allowing entry: %s", e)
        return None
