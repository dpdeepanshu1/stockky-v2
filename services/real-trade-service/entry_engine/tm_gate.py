"""entry_engine/tm_gate.py - E115: the Time Machine in LIVE stock picking, real-trade adapter (the engine is timemachine/gate_core.py, shared with position-stocks).

  Gate 4h (entry.py)  just before a BUY is staged: replays THIS trade's own stop % / target % at this time of day on the stock's recent 5-minute sessions and on
                      made-up sessions. AGAINST (both negative after charges and slippage) = the entry becomes a WAIT with gate_tag "time_machine". It can only say NO.
                      No data / slow data = no opinion and the entry goes on.
  rank_points(ev)     the shortlist composite gets +/- TM_PICK_NUDGE_PTS from the same verdict (ranking only, never a gate).
  note_shortlist(..)  the picks of a built shortlist are queued for a background verdict (trigger) and kept on the timer.
  start() / stop()    the worker (warms verdicts in the background) and the timer (re-checks the current picks every TM_AUTO_INTERVAL_MIN minutes).
TM_GATE_MODE: live | log | off (Switches page). Applies to REAL and DEMO. The replay is a same-day trade flat by EOD_SQUAREOFF_TIME_IST (a reality check for the
entry timing, not a model of a carried position).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Optional, Tuple

from timemachine import gate_core as core

logger = logging.getLogger("real-trade-tm-gate")

TAG = core.TAG


def _charges(entry: float, exit_: float, qty: int, short: bool) -> float:
    """Round-trip rupee charges of a same-day long, from the engine's own cost model."""
    import cost_model
    return float(cost_model.estimate_round_trip_cost(float(entry), int(qty), exit_price=float(exit_), product_type="INTRADAY").total)


core.configure(charges_fn=_charges, service="real-trade-service")


def mode() -> str:
    return core.mode()


def evaluate(symbol: str, stop_pct: Any, target_pct: Any, price: Any, value_rs: Any = None) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """(refusal reason or None, the evaluation or None). Blocking (may wait up to TM_GATE_INLINE_BUDGET_S): call it through asyncio.to_thread. Never raises."""
    try:
        if core.mode() == "off":
            return None, None
        core.note(symbol, "LONG", stop_pct, target_pct, value_rs, price, source="entry", enqueue=False)
        ev, reason = core.check(symbol, "LONG", stop_pct, target_pct, price, value_rs)
        return reason, ev
    except Exception as e:  # noqa: BLE001
        logger.debug("tm gate evaluate failed (allowing): %s", e)
        return None, None


def rank_points(ev: Optional[Dict[str, Any]]) -> float:
    """Ranking points from an evaluation already made in this cycle (no extra work). 0 without a verdict. Never raises."""
    try:
        v = (ev or {}).get("v")
        if not v:
            return 0.0
        pts = float(getattr(core.config, "TM_PICK_NUDGE_PTS", 6.0) or 0.0)
        if pts <= 0 or core.mode() == "off":
            return 0.0
        return pts if v.get("code") == "FAVOURS" else (-pts if v.get("code") == "AGAINST" else 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def note_shortlist(entries: Iterable[Dict[str, Any]]) -> int:
    """entries: approved entries of a cycle ({cand, entry_price, stop_price, target_price}). Queues a background verdict for each. Never raises, never waits."""
    n = 0
    try:
        if core.mode() == "off":
            return 0
        for e in list(entries)[:20]:
            try:
                ep, sp, tp = float(e["entry_price"]), float(e["stop_price"]), float(e["target_price"])
                if ep > 0 and core.note(e["cand"].symbol, "LONG", (ep - sp) / ep * 100.0, (tp - ep) / ep * 100.0, None, ep, source="shortlist"):
                    n += 1
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return n


def stamp(text: Optional[str], ev: Optional[Dict[str, Any]]) -> Optional[str]:
    return core.attach(text, ev)


def scan(reason: str = "manual", wait: bool = False) -> Dict[str, Any]:
    return core.scan(reason, wait=wait)


def status() -> Dict[str, Any]:
    return core.status()


def start() -> bool:
    return core.start()


def stop() -> None:
    core.stop()
