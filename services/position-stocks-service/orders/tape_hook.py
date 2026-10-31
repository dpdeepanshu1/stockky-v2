"""orders/tape_hook.py - apply the order-flow tape to OPEN trades' stop and target (E270, 30 Oct 2026).

One pass per fast-reconcile cycle (main.py), after trailing and BEFORE the stop watchdog / plain-short guard, so Super Order longs, Super Order shorts and
plain shorts all get it from one place. The decision is the pure `tape_exit.decide`; this file only reads the live price, asks `flow_gate.exit_check`
and applies a TIGHTEN:
  * Super Order position: STOP_LOSS_LEG / TARGET_LEG modify through dhan_client.modify_super_order, the stop read back with verify_stop_leg (an unconfirmed
    modify leaves the stored value alone, logs and retries next cycle);
  * plain short (no Super Order, `plain_stop_order_id`): ONLY `pos.target_price` moves (the software target run_plain_short_guard covers at). The broker
    stop is never touched (E114: a plain short's stop is fixed by design);
  * a stop is never loosened, a target never raised (also checked here, in case the pure function is ever changed);
  * FLOW_EXIT_MODE=log only logs the decision; off does nothing. Per-position FLOW_EXIT_MIN_INTERVAL_S throttle. Never raises, never exits a trade by itself.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

import config
from execution import dhan_client
from models import ScalpPosition
from orders import flow_gate, side as side_mod

logger = logging.getLogger("position-stocks-tape-hook")


def _ltp(symbol: str):
    try:
        from feed import ws_client
        buf = ws_client.get_tick_buffer(symbol)
        v = buf[-1][1] if buf else None
        return float(v) if v and v > 0 else None
    except Exception:  # noqa: BLE001
        return None


def run_tape_exit(db: Session) -> int:
    """Returns the number of positions whose stop and/or target were moved this pass."""
    if flow_gate.exit_mode() == "off":
        return 0
    moved = 0
    try:
        positions = db.query(ScalpPosition).filter(ScalpPosition.status == "OPEN").all()
    except Exception as e:  # noqa: BLE001
        logger.warning("tape-exit: could not load positions: %s", e)
        return 0
    for pos in positions:
        try:
            if getattr(pos, "overnight_converted_to_cnc", False):
                continue
            key = ("ps", pos.id)
            if flow_gate.exit_throttled(key):
                continue
            ltp = _ltp(pos.symbol)
            if ltp is None:
                continue
            short = side_mod.is_short(pos)
            plain = bool(getattr(pos, "plain_stop_order_id", None)) and not getattr(pos, "dhan_super_order_id", None)
            if not plain and not getattr(pos, "dhan_super_order_id", None):
                continue                                   # nothing we can move
            tick = dhan_client.tick_size_for_price(ltp)
            d = flow_gate.exit_check(pos.symbol, "SHORT" if short else "LONG", entry=pos.entry_price, stop=pos.stop_price,
                                     target=pos.target_price, ltp=ltp, tick=tick)
            if not d or d.get("action") != "TIGHTEN":
                continue
            new_stop, new_target = d.get("new_stop"), d.get("new_target")
            s = -1 if short else 1
            if plain:
                new_stop = None                            # E114: never touch a plain short's broker stop
            if new_stop is not None and pos.stop_price is not None and s * (float(new_stop) - float(pos.stop_price)) <= 0:
                new_stop = None                            # never loosen
            if new_target is not None and pos.target_price is not None and s * (float(new_target) - float(pos.target_price)) >= 0:
                new_target = None                          # never raise (long) / lower (short) the target
            if new_stop is None and new_target is None:
                continue
            if flow_gate.exit_mode() != "live":
                logger.info("TAPE_EXIT[log]: %s (id=%d) would move stop %s target %s - %s", pos.symbol, pos.id, new_stop, new_target, d.get("reason"))
                flow_gate.exit_mark(key)
                continue
            flow_gate.exit_mark(key)
            d_old_stop, d_old_target = pos.stop_price, pos.target_price
            changed = False
            if new_stop is not None:
                try:
                    dhan_client.modify_super_order(db, order_id=pos.dhan_super_order_id, order_leg="STOP_LOSS_LEG", stop_loss_price=new_stop)
                    if dhan_client.verify_stop_leg(db, order_id=pos.dhan_super_order_id, expected_price=new_stop) is False:
                        logger.warning("TAPE_EXIT: %s (id=%d) stop modify to %.2f NOT confirmed by Dhan - stored stop left at %s", pos.symbol, pos.id, new_stop, pos.stop_price)
                    else:
                        pos.stop_price = new_stop
                        pos.stop_moved_to_breakeven = True   # so breakeven.py can never lower it again
                        changed = True
                except Exception as e:  # noqa: BLE001
                    logger.warning("TAPE_EXIT: %s (id=%d) stop modify to %.2f rejected (%s) - leg unchanged", pos.symbol, pos.id, new_stop, e)
            if new_target is not None:
                if plain:
                    pos.target_price = new_target
                    changed = True
                else:
                    try:
                        dhan_client.modify_super_order(db, order_id=pos.dhan_super_order_id, order_leg="TARGET_LEG", target_price=new_target)
                        pos.target_price = new_target
                        changed = True
                    except Exception as e:  # noqa: BLE001
                        logger.warning("TAPE_EXIT: %s (id=%d) target modify to %.2f rejected (%s) - leg unchanged", pos.symbol, pos.id, new_target, e)
            if changed:
                try:   # E271: record the move on the trade (record only; a failure here never undoes the move)
                    pos.flow_json = flow_gate.stamp_tape_exit(getattr(pos, "flow_json", None), {
                        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "ltp": round(float(ltp), 2), "old_stop": d_old_stop,
                        "new_stop": pos.stop_price, "old_target": d_old_target, "new_target": pos.target_price, "reason": str(d.get("reason") or "")[:160],
                        "plain": bool(plain)})
                except Exception as e:  # noqa: BLE001
                    logger.debug("tape-exit: stamp failed for %s: %s", pos.symbol, e)
                flow_gate.exit_applied()
                db.commit()
                moved += 1
                logger.info("TAPE_EXIT: %s (id=%d) %s -> stop %s target %s (ltp %.2f)", pos.symbol, pos.id, d.get("reason"), pos.stop_price, pos.target_price, ltp)
        except Exception as e:  # noqa: BLE001
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.warning("tape-exit: %s failed: %s", getattr(pos, "symbol", "?"), e)
    return moved
