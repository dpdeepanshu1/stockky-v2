"""Close-all and disarm (group 466) for position-stocks-service.

Sweeps every live scalp position through orders.eod_squareoff.close_position_now() - the same proven path as the manual
close, the EOD square-off and the stagnation exit (cancel the Super Order legs, then a plain MARKET SELL with the retry /
cooldown / already-flat guards). Nothing here places an order itself.

The route (main.py POST /close-all) owns the confirm gate, the disarm-first order and the cycle lock; this module is the
blocking worker-thread part: disarm(), sweep(), merge() for the second pass, plus the summary / notification text.
Positions are stored with status CLOSE_ALL (orders/statuses.py FLAT_SELL_STATUSES), so reconcile resolves the real fill
price the same way it does for EOD_SQUAREOFF / MANUAL_EXIT.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

CONFIRM_PHRASE = "CLOSE ALL"
CLOSE_REASON = "CLOSE_ALL"
OPEN_STATUSES = ("OPEN", "EXIT_LEGS_REJECTED")


def confirm_ok(value) -> bool:
    """The typed confirmation: case-insensitive, whitespace-trimmed, exact otherwise."""
    return isinstance(value, str) and value.strip().upper() == CONFIRM_PHRASE


def disarm(db, get_gate) -> bool:
    """Set is_armed=False. Returns True if it was armed. Never raises (a failed disarm must not stop the sweep)."""
    try:
        gate = get_gate(db)
        was_armed = bool(gate.is_armed)
        gate.is_armed = False
        db.commit()
        return was_armed
    except Exception:  # noqa: BLE001
        logger.exception("close-all: disarm failed")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            logger.debug("close-all: rollback after failed disarm also failed", exc_info=True)
        return False


def sweep(db) -> dict:
    """One pass: close_position_now() on every OPEN / EXIT_LEGS_REJECTED position. Returns
    {"found", "sent", "symbols": [...], "failed": [{"symbol", "error"}]}. Never raises."""
    from models import ScalpPosition
    from orders.eod_squareoff import close_position_now, ManualCloseRejected
    res = {"found": 0, "sent": 0, "symbols": [], "failed": []}
    try:
        db.expire_all()
        positions = db.query(ScalpPosition).filter(ScalpPosition.status.in_(OPEN_STATUSES)).all()
    except Exception:  # noqa: BLE001
        logger.exception("close-all: could not list open positions")
        res["failed"].append({"symbol": "<query failed>", "error": "position query failed"})
        return res
    res["found"] = len(positions)
    for pos in positions:
        sym = pos.symbol
        try:
            close_position_now(db, pos, exit_reason=CLOSE_REASON)
            res["sent"] += 1
            res["symbols"].append(sym)
        except ManualCloseRejected as e:
            logger.warning("close-all: %s rejected: %s", sym, e)
            res["failed"].append({"symbol": sym, "error": str(e)[:200]})
        except Exception as e:  # noqa: BLE001 - one bad position must not stop the rest
            logger.exception("close-all: close of %s failed", sym)
            res["failed"].append({"symbol": sym, "error": str(e)[:200]})
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                logger.debug("close-all: rollback after failed close of %s also failed", sym, exc_info=True)
    return res


def merge(first: dict, second: dict) -> dict:
    """Combine two sweep passes: a symbol sold in either pass is sold; a failure in pass 1 that pass 2 fixed is dropped."""
    sold = list(dict.fromkeys(list(first.get("symbols", [])) + list(second.get("symbols", []))))
    failed_by_sym = {f["symbol"]: f for f in first.get("failed", [])}
    for f in second.get("failed", []):
        failed_by_sym[f["symbol"]] = f
    for sym in sold:
        failed_by_sym.pop(sym, None)
    seen = set(sold) | set(failed_by_sym)
    return {"found": len(seen), "sent": len(sold), "failed": list(failed_by_sym.values()), "symbols": sold}


def summary(r: dict) -> str:
    parts = [f"{r.get('sent', 0)} of {r.get('found', 0)} position(s) sold"]
    if r.get("failed"):
        names = ", ".join((f.get("symbol") if isinstance(f, dict) else str(f)) for f in r["failed"])
        parts.append(f"FAILED: {names} - close manually")
    return "; ".join(parts)


def notify(actor: str, text: str) -> None:
    try:
        from notifier import notify_fire_and_forget
        notify_fire_and_forget(f"🚨 <b>CLOSE ALL - position-stocks</b>\nby {actor}\n{text}")
    except Exception:  # noqa: BLE001 - an alert must never be the reason this fails
        logger.debug("close-all: notify failed", exc_info=True)
