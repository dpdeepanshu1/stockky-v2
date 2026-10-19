"""Close-all and disarm (group 466). The EMERGENCY CLOSE ALL action that the /emergency-pause docstring promised and
that was never built: until now the only panic button was "pause new trades", which leaves every open position running.

WHAT ONE CALL DOES, IN THIS ORDER (each step is independent, so a failure in one never stops the next):
  1. DISARM the mode first. Disarming only ever reduces risk and the auto-pilot tick re-reads `armed` from the DB every pass,
     so no new entry can start while the rest of this runs. This happens even if the locks below cannot be taken.
  2. Take the per-mode entry lock and exit lock (the same two, in the same fixed order, as /orders/{mode}/{id}/cancel and
     cycle_runner, so this can never deadlock against them). Waits up to CLOSE_ALL_LOCK_WAIT_S for a running cycle to finish;
     if it still cannot get them it reports `locks_busy` (409 from the route) and the mode stays disarmed.
  3. CANCEL every resting BUY (PENDING / PLACED / PARTIAL) so nothing fills after we have flattened. REAL cancels at Dhan first and
     only marks the row CANCELLED when Dhan accepted; a cancel that fails is counted and named, never silently dropped.
  4. SELL every OPEN / PARTIALLY_CLOSED position through the EXISTING sell path: REAL = exit_engine._send_real_sell (MARKET, which
     already cancels the resting broker stop first and picks CNC / INTRADAY) behind the same pending-SELL guard
     (_has_pending_real_sell) the EOD square-off and the single-position close use; DEMO = portfolio.close_position at the live tick.
     A position whose SELL is already working at the broker is SKIPPED, not double-sold (the pending SELL is already flattening it).

Always allowed regardless of armed state (exiting is never refused), same policy as manual close. Runs on a worker thread (the
caller does `await asyncio.to_thread(...)`) because _send_real_sell / dhan_client.cancel_order are synchronous broker I/O.

The confirm gate lives in the route (body.confirm must equal CONFIRM_PHRASE); this module takes `confirmed=True` as a second
belt so a stray internal call cannot flatten the book by accident.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

CONFIRM_PHRASE = "CLOSE ALL"
CLOSE_REASON = "close_all"
RESTING_BUY_STATUSES = ("PENDING", "PLACED", "PARTIAL")


def _lock_wait_s() -> float:
    try:
        return max(0.0, float(os.environ.get("CLOSE_ALL_LOCK_WAIT_S", "20") or 20))
    except ValueError:
        return 20.0


def confirm_ok(value) -> bool:
    """The typed confirmation: case-insensitive, whitespace-trimmed, and exact otherwise."""
    return isinstance(value, str) and value.strip().upper() == CONFIRM_PHRASE


def disarm_mode(db, mode: str, actor: str, reason: str) -> bool:
    """Set armed=False for `mode` (same fields main._disarm writes). Returns True if it was armed. Never raises."""
    import models
    from audit.logger import log_action
    try:
        gate = db.query(models.TradeGateState).filter_by(mode=mode).first()
        if gate is None:
            return False
        was_armed = bool(gate.armed)
        gate.armed = False
        gate.disarmed_reason = reason
        gate.updated_at = datetime.now(timezone.utc)
        db.commit()
        if was_armed:
            log_action(db, actor=actor, action="DISARMED", detail=reason, mode=mode)
        return was_armed
    except Exception:  # noqa: BLE001 - disarm is best effort here, the caller reports it
        logger.exception("close-all: disarm failed for %s", mode)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def cancel_resting_buys(db, mode: str) -> dict:
    """Cancel every resting BUY for `mode`. Returns {"cancelled": n, "failed": [..symbols..]}."""
    import models
    from execution import dhan_client
    out = {"cancelled": 0, "failed": []}
    try:
        rows = (
            db.query(models.TradeOrder)
            .filter(models.TradeOrder.mode == mode, models.TradeOrder.side == "BUY",
                    models.TradeOrder.status.in_(RESTING_BUY_STATUSES))
            .all()
        )
    except Exception:  # noqa: BLE001
        logger.exception("close-all: could not list resting BUY orders for %s", mode)
        return {"cancelled": 0, "failed": ["<query failed>"]}
    for o in rows:
        try:
            if mode == "REAL" and o.dhan_order_id:
                dhan_client.cancel_order(db, is_armed=True, dhan_order_id=o.dhan_order_id)
            o.status = "CANCELLED"
            o.updated_at = datetime.now(timezone.utc)
            db.add(models.TradeOrderEvent(order_id=o.id, event_type="CANCELLED", detail="Cancelled by close-all and disarm"))
            db.commit()
            out["cancelled"] += 1
        except Exception as e:  # noqa: BLE001 - one bad cancel must not stop the rest
            logger.warning("close-all: cancel of BUY %s (%s) failed: %s", o.id, o.symbol, e)
            out["failed"].append(o.symbol)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
    return out


def sell_all_positions(db, mode: str, ticks: Optional[dict] = None) -> dict:
    """Flatten every live position for `mode` through the existing sell path. Returns counts plus per-symbol detail."""
    from portfolio.portfolio import open_positions as _open_positions, close_position as _close_position
    positions = list(_open_positions(db, mode))
    res = {"found": len(positions), "closed": 0, "sent": 0, "skipped_pending": [], "failed": [], "symbols": []}
    if mode == "DEMO":
        ticks = ticks or {}
        for p in positions:
            tick = ticks.get(p.symbol)
            if tick is None:
                res["failed"].append(p.symbol)
                continue
            try:
                _close_position(db, p, tick, p.qty_open, CLOSE_REASON)
                res["closed"] += 1
                res["symbols"].append(p.symbol)
            except Exception:  # noqa: BLE001
                logger.exception("close-all: DEMO close failed for %s", p.symbol)
                res["failed"].append(p.symbol)
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
        return res

    from exit_engine.exit import _send_real_sell, _has_pending_real_sell
    for p in positions:
        try:
            if _has_pending_real_sell(db, p.symbol):
                res["skipped_pending"].append(p.symbol)
                continue
            if _send_real_sell(db, p, p.qty_open, CLOSE_REASON, full=True):
                res["sent"] += 1
                res["symbols"].append(p.symbol)
            else:
                res["failed"].append(p.symbol)
        except Exception:  # noqa: BLE001
            logger.exception("close-all: REAL sell failed for %s", p.symbol)
            res["failed"].append(p.symbol)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
    return res


def run_close_all(db, mode: str, *, actor: str, confirmed: bool, ticks: Optional[dict] = None,
                  lock_wait_s: Optional[float] = None) -> dict:
    """The whole sequence. Blocking: call from a worker thread. Never raises; the result says what happened.

    result keys: ok, mode, disarmed, was_armed, locks_busy, buys_cancelled, buy_cancel_failed, found, closed, sent,
    skipped_pending, failed, summary.
    """
    mode = (mode or "").upper()
    base = {"ok": False, "mode": mode, "disarmed": False, "was_armed": False, "locks_busy": False,
            "buys_cancelled": 0, "buy_cancel_failed": [], "found": 0, "closed": 0, "sent": 0,
            "skipped_pending": [], "failed": [], "summary": ""}
    if mode not in ("DEMO", "REAL"):
        base["summary"] = "mode must be DEMO or REAL"
        return base
    if not confirmed:
        base["summary"] = "not confirmed"
        return base

    base["was_armed"] = disarm_mode(db, mode, actor, f"Close-all and disarm by {actor}")
    base["disarmed"] = True

    from execution.auto_pilot import _get_lock, _get_exit_lock
    entry_lock, exit_lock = _get_lock(mode), _get_exit_lock(mode)
    wait = _lock_wait_s() if lock_wait_s is None else lock_wait_s
    got_entry = entry_lock.acquire(timeout=wait) if wait > 0 else entry_lock.acquire(blocking=False)
    got_exit = False
    try:
        if got_entry:
            got_exit = exit_lock.acquire(timeout=wait) if wait > 0 else exit_lock.acquire(blocking=False)
        if not (got_entry and got_exit):
            base["locks_busy"] = True
            base["summary"] = (f"{mode} disarmed, but a cycle is still running and holds the lock - nothing was sold. "
                               "Press Close all again in a moment.")
            _audit(db, actor, mode, base)
            return base
        try:
            db.expire_all()
        except Exception:  # noqa: BLE001
            pass
        c = cancel_resting_buys(db, mode)
        base["buys_cancelled"], base["buy_cancel_failed"] = c["cancelled"], c["failed"]
        s = sell_all_positions(db, mode, ticks)
        for k in ("found", "closed", "sent", "skipped_pending", "failed"):
            base[k] = s[k]
    finally:
        if got_exit:
            exit_lock.release()
        if got_entry:
            entry_lock.release()

    base["ok"] = not base["failed"] and not base["buy_cancel_failed"]
    base["summary"] = _summary(base)
    _audit(db, actor, mode, base)
    return base


def _summary(r: dict) -> str:
    done = r["closed"] if r["mode"] == "DEMO" else r["sent"]
    verb = "closed" if r["mode"] == "DEMO" else "sell order(s) sent"
    parts = [f"{r['mode']} disarmed", f"{done} of {r['found']} position(s) {verb}"]
    if r["buys_cancelled"]:
        parts.append(f"{r['buys_cancelled']} resting BUY cancelled")
    if r["skipped_pending"]:
        parts.append(f"{len(r['skipped_pending'])} skipped, SELL already pending ({', '.join(r['skipped_pending'])})")
    if r["failed"]:
        parts.append(f"FAILED: {', '.join(r['failed'])} - close manually")
    if r["buy_cancel_failed"]:
        parts.append(f"BUY cancel FAILED: {', '.join(r['buy_cancel_failed'])}")
    return "; ".join(parts)


def _audit(db, actor: str, mode: str, r: dict) -> None:
    try:
        from audit.logger import log_action
        log_action(db, actor=actor, action="CLOSE_ALL", mode=mode, detail=r["summary"][:900])
    except Exception:  # noqa: BLE001
        logger.exception("close-all: audit write failed")
    try:
        import notifier
        notifier.notify_sync(f"🚨 *CLOSE ALL - {mode}*\n{r['summary']}")
    except Exception:  # noqa: BLE001 - an alert must never be the reason this fails
        logger.debug("close-all: notify failed", exc_info=True)
