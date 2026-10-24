"""orders/tm_gate.py - E115: the Time Machine in LIVE stock picking, position-stocks adapter (the engine is timemachine/gate_core.py, shared with real-trade).

  check_ev / entry_check  the gate on a long or a short entry: refuses when this trade's own stop % / target % at this time of day shows AGAINST (simulation and the
                          stock's real recent 5-minute sessions both negative after charges); otherwise stamps the verdict into flow_json["tm"]. Never raises.
  rank_nudge(...)         ranking points for the shortlist (cache only).
  note_picks(...)         tells the background warmer which stocks are being considered, with their levels, so their verdicts are ready (and re-checked on the timer).
  start() / stop()        the worker and the timer.
TM_GATE_MODE: live | log | off (Switches page). It only ever says NO: it adds no trade, widens no stop and raises no size.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Optional, Tuple

from timemachine import gate_core as core

logger = logging.getLogger("position-stocks-tm-gate")

TAG = core.TAG


def _charges(entry: float, exit_: float, qty: int, short: bool) -> float:
    from orders import charges_ledger
    return float(charges_ledger.charges_for_trade(entry, exit_, qty, short=short)["total"])


core.configure(charges_fn=_charges, service="position-stocks-service")


def mode() -> str:
    return core.mode()


def check_ev(symbol: str, side: str, stop_pct: Any, target_pct: Any, price: Any, value_rs: Any = None) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """(refusal reason or None, the evaluation or None). Registers the pick for the timer. Never raises."""
    try:
        if core.mode() == "off":
            return None, None
        core.note(symbol, side, stop_pct, target_pct, value_rs, price, source="entry", enqueue=False)
        ev, reason = core.check(symbol, side, stop_pct, target_pct, price, value_rs)
        return reason, ev
    except Exception as e:  # noqa: BLE001
        logger.debug("tm gate check_ev failed (allowing): %s", e)
        return None, None


def stamp(flow_json: Optional[str], ev: Optional[Dict[str, Any]]) -> Optional[str]:
    return core.attach(flow_json, ev)


def entry_check(symbol: str, side: str, stop_pct: Any, target_pct: Any, price: Any, flow_json: Optional[str] = None,
                value_rs: Any = None) -> Tuple[Optional[str], Optional[str]]:
    """(refusal reason or None, flow_json with the verdict stamped). Never raises."""
    reason, ev = check_ev(symbol, side, stop_pct, target_pct, price, value_rs)
    return reason, core.attach(flow_json, ev)


def rank_nudge(symbol: str, side: str = "LONG") -> float:
    return core.nudge(symbol, side)


def note_picks(passers: Iterable[Tuple[Any, Any]], side: str = "LONG") -> int:
    """passers: [(candidate, quality)] about to be ranked. The candidate's own levels are worked out the way the entry will work them out. Never raises."""
    n = 0
    try:
        if core.mode() == "off":
            return 0
        from orders.adaptive import compute as compute_levels
        for c, _q in list(passers)[:20]:
            try:
                lv = compute_levels(c.pct_change, c.current_ltp, symbol=c.symbol, side=side)
                if core.note(c.symbol, side, lv.stop_pct, lv.target_pct, None, c.current_ltp, source="shortlist"):
                    n += 1
            except Exception:  # noqa: BLE001
                continue
    except Exception as e:  # noqa: BLE001
        logger.debug("tm gate note_picks failed (ignored): %s", e)
    return n


def scan(reason: str = "manual", wait: bool = False) -> Dict[str, Any]:
    return core.scan(reason, wait=wait)


def status() -> Dict[str, Any]:
    return core.status()


def start() -> bool:
    return core.start()


def stop() -> None:
    core.stop()
