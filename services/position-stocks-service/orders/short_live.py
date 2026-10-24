"""orders/short_live.py - the LIVE short entry (short selling step 8 + 9, 2026-10-24). Position Stocks only.

WHAT: with SHORT_MODE=live, the short cycle (orders/short_entry.run_short_cycle) hands each candidate here. The candidate walks the SAME mirrored gates, levels and
sizing as a shadow short (orders/short_entry.plan_short, one shared function so the two cannot drift), then the live-only checks (armed, daily-loss kill switch,
order budget, margin pause, entry caps, concurrent positions, symbol lock, capital, shared order budget), and a real SELL Super Order is placed WITH its broker
stop leg and target leg. The position is stored with side="SHORT"; the exit side (orders/side.py, eod_squareoff, reconcile, stop_watchdog) already closes it with a BUY.

SAFETY RAILS (all fail closed):
  * size: never more than SHORT_LIVE_QTY_CAP shares (ships 1; only you raise it; protected from the calibrator).
  * the broker stop is mandatory. The normal way is the SELL Super Order, whose stop leg is part of the order. E114: when Dhan refuses it (or USE_SUPER_ORDER is off) and
    SHORT_PLAIN_FALLBACK allows it, orders/plain_short.py opens the short as a plain MARKET SELL and places a BUY stop-loss-market order at the broker at once; if that stop
    cannot be placed the short is covered immediately and no plain short is tried again today. Refusals a plain SELL would meet too (restricted stock, circuit limit,
    intraday cut-off, no funds) and errors that may have created the Super Order anyway never fall back. A plain short has a fixed stop (no trailing) and is looked after by
    plain_short.run_plain_short_guard (software target, breach cover, 14:55 cover).
  * capital goes through ledger.reserve_capital(side="SHORT") (refuses unless SHORT_MODE is live, applies SHORT_RISK_MULT and the Market Weather short dial).
  * a short is intraday only (INTRADAY product, never converted or carried; orders/side.can_carry_overnight is False; the cover is at SHORT_SQUAREOFF_TIME_IST).
  * every refusal releases whatever was claimed (capital, symbol lock) before returning.

`orders/entry.py` (the long entry) is not edited. Nothing in this file runs unless SHORT_MODE is exactly "live".
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

import config
import notifier
from capital import ledger, shared_order_budget, shared_symbol_lock
from control.kit import current_profile as _current_profile
from execution import dhan_client
from models import ScalpGateState, ScalpPosition
from orders import approve_first, entry_cap, entry_pause, opening_protect, plain_short, sell_probe, short_entry, tm_gate, trade_odds
from tz_utils import ist_today_str

logger = logging.getLogger("position-stocks-short-live")

SIDE = "SHORT"


def live_mode() -> bool:
    """True only when SHORT_MODE is exactly "live" (read at call time)."""
    return str(getattr(config, "SHORT_MODE", "off") or "off").strip().lower() == "live"


def qty_cap() -> int:
    """SHORT_LIVE_QTY_CAP, at least 1. An unreadable value counts as 1 (the smallest short), never as 'no cap'."""
    try:
        v = int(getattr(config, "SHORT_LIVE_QTY_CAP", 1))
    except Exception:  # noqa: BLE001
        return 1
    return max(1, v)


def _skip(db: Session, cand: Any, code: str, reason: str) -> None:
    short_entry._refuse(db, cand, code, reason)


def _open_counts(db: Session) -> tuple:
    rows = db.query(ScalpPosition).filter(ScalpPosition.status.in_(("OPEN", "EXIT_LEGS_REJECTED"))).all()
    shorts = [r for r in rows if str(getattr(r, "side", "") or "").strip().upper() == SIDE]
    return len(rows), len(shorts)


def _live_guards(db: Session, cand: Any) -> Optional[tuple]:
    """(code, reason) of the first live-only guard that says no, else None. Reads only; claims nothing."""
    gate = db.query(ScalpGateState).first()
    if gate is None or not getattr(gate, "is_armed", False):
        return ("SHORT_NOT_ARMED", "SERVICE_NOT_ARMED")
    if gate.daily_loss_kill_switch_tripped:
        return ("SHORT_KILL_SWITCH", "DAILY_LOSS_KILL_SWITCH")
    today = ist_today_str()
    if gate.orders_placed_today_date == today and gate.orders_placed_today >= config.DAILY_ORDER_BUDGET:
        return ("SHORT_ORDER_BUDGET", f"ORDER_BUDGET_EXHAUSTED:{gate.orders_placed_today}")
    for fn in (lambda: opening_protect.pause_reason(db), lambda: entry_cap.entry_reason(db)):
        why = fn()
        if why:
            return ("SHORT_GUARD", str(why))
    if not config.USE_SUPER_ORDER and not plain_short.entry_allowed():
        return ("SHORT_NEEDS_SUPER_ORDER", "USE_SUPER_ORDER is off and the plain-order short is not available (SHORT_PLAIN_FALLBACK off, or no broker stop could be placed earlier today): a short must have a broker stop")
    if getattr(config, "SHORT_LIVE_REQUIRES_PROBE", True) and not sell_probe.passed():
        return ("SHORT_PROBE_NOT_PASSED", "the Dhan SELL probe (T6) has no PASS on record yet: run the Dhan SELL probe job first (or set SHORT_LIVE_REQUIRES_PROBE=false)")
    if approve_first.active():
        return ("SHORT_APPROVAL_ROUND", "an approve-first round is open; shorts wait until it is over")
    n_open, n_short = _open_counts(db)
    if n_open >= config.MAX_CONCURRENT_SCALP_POSITIONS:
        return ("SHORT_MAX_POSITIONS", f"MAX_POSITIONS:{n_open}")
    if n_short >= int(config.SHORT_MAX_POSITIONS):
        return ("SHORT_MAX_POSITIONS", f"{n_short} live shorts open (max {config.SHORT_MAX_POSITIONS})")
    return None


def attempt_short_live(db: Session, cand: Any, quality: Any = None, board_row: Optional[dict] = None,
                       now: Optional[datetime] = None) -> Optional[ScalpPosition]:
    """Try to open ONE live short. Returns the ScalpPosition or None. Never raises: an error before the order is sent opens nothing; an error after it is sent
    is reported loudly (the order exists at the broker and reconcile picks it up)."""
    if not live_mode():
        return None
    try:
        return _attempt(db, cand, quality, board_row, now)
    except Exception as e:  # noqa: BLE001
        logger.error("short live: attempt for %s failed (%s: %s)", getattr(cand, "symbol", "?"), type(e).__name__, e, exc_info=True)
        try:
            db.rollback()
        except Exception as _e:  # noqa: BLE001
            logger.debug("short_live.py: rollback ignored %s", _e)
        return None


def _attempt(db: Session, cand: Any, quality: Any, board_row: Optional[dict], now: Optional[datetime]) -> Optional[ScalpPosition]:
    sym = cand.symbol
    why = _live_guards(db, cand)
    if why:
        _skip(db, cand, why[0], why[1])
        return None

    if not shared_symbol_lock.try_claim(db, sym):
        _skip(db, cand, "SHORT_SYMBOL_HELD_ELSEWHERE", f"{sym} is held by the other service")
        return None
    claimed = True
    position_value: Optional[float] = None       # what the ledger has reserved (None = nothing)

    def _undo() -> None:
        nonlocal position_value, claimed
        if position_value is not None:
            try:
                ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
            except Exception as e:  # noqa: BLE001
                logger.error("short live: capital release for %s failed (%s)", sym, e)
            position_value = None
        if claimed:
            try:
                shared_symbol_lock.release(db, sym)
            except Exception as e:  # noqa: BLE001
                logger.error("short live: symbol lock release for %s failed (%s)", sym, e)
            claimed = False

    try:
        plan = short_entry.plan_short(db, cand, quality, board_row, now)
        if plan is None:
            _undo()
            return None
        live, levels, conv = plan["live"], plan["levels"], plan["conv"]
        qty = min(int(plan["qty"]), qty_cap())
        if qty < 1:
            _skip(db, cand, "SHORT_QTY", "quantity below one share after the live size cap")
            _undo()
            return None

        position_value = ledger.reserve_capital(db, adaptive_stop_pct=float(plan.get("budget_stop_pct") or levels.stop_pct), side="SHORT")
        if position_value is None:
            _skip(db, cand, "SHORT_NO_CAPITAL", "ledger refused the short (mode, kill switch, dial or pool)")
            _undo()
            return None
        actual_cost = qty * live
        if actual_cost < position_value:                       # the size cap shrank the order: give the unused reservation back
            ledger.release_capital(db, position_value=position_value - actual_cost, realized_pnl=0.0)
            position_value = actual_cost
        elif actual_cost > position_value:
            if not ledger.reserve_additional(db, actual_cost - position_value):
                _skip(db, cand, "SHORT_NO_CAPITAL", f"shortfall {actual_cost - position_value:.2f} for {qty} share(s)")
                _undo()
                return None
            position_value = actual_cost

        try:
            security_id = dhan_client.get_security_id(db, sym)
        except dhan_client.SecurityNotResolvedError as e:
            _skip(db, cand, "SHORT_SECURITY_NOT_FOUND", str(e))
            _undo()
            return None
        if not security_id:
            _skip(db, cand, "SHORT_SECURITY_NOT_FOUND", f"no Dhan security id for {sym}")
            _undo()
            return None

        if not shared_order_budget.check_and_reserve(db):
            _skip(db, cand, "SHORT_SHARED_ORDER_BUDGET", "SHARED_ORDER_BUDGET_EXHAUSTED")
            _undo()
            return None
    except Exception:
        _undo()
        raise

    # ---- the order. From here an exception must NOT release anything blindly: the order may exist at the broker. ----
    gate = db.query(ScalpGateState).first()
    super_id, error_msg, ambiguous = None, None, False
    plain: Optional[dict] = None
    if config.USE_SUPER_ORDER:
        try:
            result = dhan_client.place_super_order(
                db, is_armed=bool(gate and gate.is_armed), security_id=security_id, exchange_segment=config.SCALP_EXCHANGE_SEGMENT,
                transaction_type="SELL", quantity=int(qty), order_type="MARKET", price=live,
                target_price=float(levels.target_price), stop_loss_price=float(levels.stop_price), trailing_jump=0.0,
                product_type="INTRADAY", tag="SCALP")
            super_id = str((result or {}).get("orderId") or (result or {}).get("id") or "")
            if not super_id:
                error_msg = "Dhan answered without an order id"
        except Exception as e:  # noqa: BLE001
            rec = _recover_ambiguous_sell(db, e, security_id, qty, sym)
            if rec:
                super_id = rec
            else:
                error_msg = str(e)
                try:
                    ambiguous = bool(dhan_client.is_ambiguous_placement_error(e))
                except Exception:  # noqa: BLE001
                    ambiguous = True                         # cannot tell: the Super Order may exist, so no second order
    # E114: a refused Super Order (or Super Orders off) may be retried as a plain SELL that gets its broker stop at once
    if (error_msg is not None and plain_short.refusal_allows_fallback(error_msg, ambiguous)) or (not config.USE_SUPER_ORDER and plain_short.entry_allowed()):
        logger.warning("short live: %s Super Order %s - trying the plain-order short (E114)", sym, "refused: " + str(error_msg)[:160] if error_msg else "is off")
        plain = plain_short.open_plain_short(
            db, is_armed=bool(gate and gate.is_armed), security_id=security_id, sym=sym, qty=int(qty), live=live,
            stop_price=float(levels.stop_price), target_price=float(levels.target_price), stop_pct=float(levels.stop_pct), target_pct=float(levels.target_pct))
        error_msg = None
        if plain.get("critical") and not plain.get("ok"):
            # the SELL may exist and its state is unknown: keep the capital and the lock, hand the SELL to the watch list (it releases them when the SELL resolves)
            plain_short.watch_orphan(plain.get("entry_order_id"), sym, security_id, int(qty), position_value or 0.0)
            short_entry._refuse(db, cand, "SHORT_PLAIN_UNKNOWN", str(plain.get("reason"))[:400])
            return None                                   # nothing is released here on purpose: the order may exist at Dhan
        if not plain.get("ok"):
            if plain.get("covered"):
                short_entry._refuse(db, cand, "SHORT_PLAIN_STOP_FAILED", str(plain.get("reason"))[:400])
                _undo()
                return None
            error_msg = str(plain.get("reason") or "the plain SELL failed")
        else:
            got = int(plain.get("qty") or qty)
            if got < int(qty) and position_value:                  # part filled: give back the reservation of the shares that never filled
                keep = position_value * got / float(qty)
                try:
                    ledger.release_capital(db, position_value=position_value - keep, realized_pnl=0.0)
                    position_value = keep
                except Exception as e:  # noqa: BLE001
                    logger.error("short live: partial-fill capital release for %s failed (%s)", sym, e)
            qty = got
            live = float(plain.get("fill_price") or live)
            levels.stop_price = float(plain.get("stop_price") or levels.stop_price)       # the levels plain_short really sent / computed from the real fill (tick-rounded)
            levels.target_price = float(plain.get("target_price") or levels.target_price)

    if error_msg is not None:
        _handle_failure(db, cand, sym, error_msg)
        _undo()
        return None

    # the order is out: count it, then record the position
    today = ist_today_str()
    try:
        if gate.orders_placed_today_date != today:
            gate.orders_placed_today, gate.orders_placed_today_date = 0, today
        gate.orders_placed_today += 1
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("short live: order-budget count for %s failed (%s)", sym, e)
        db.rollback()
    prof = _current_profile() or {}
    try:
        pos = ScalpPosition(
            market_state=prof.get("market_state"), profile_version=prof.get("profile_version"),
            symbol=sym, dhan_security_id=security_id, window_source=str(getattr(cand, "window_label", "") or "")[:8], status="OPEN", side=SIDE,
            entry_price=live, signal_price=float(cand.current_ltp), quantity=int(qty),
            target_price=float(levels.target_price), stop_price=float(levels.stop_price),
            adaptive_target_pct=float(levels.target_pct), adaptive_stop_pct=float(levels.stop_pct),
            breakeven_trigger_pct=levels.breakeven_trigger_pct,
            flow_json=trade_odds.attach(None, sym, SIDE, levels.stop_pct, levels.target_pct),   # E103: record only, never raises
            dhan_super_order_id=None if plain else super_id, dhan_entry_order_id=(plain or {}).get("entry_order_id") or super_id,
            plain_stop_order_id=(plain or {}).get("stop_order_id"), capital_risked=position_value,
            is_first_live_order=False, opened_at=datetime.now(timezone.utc))
        pos.flow_json = tm_gate.stamp(pos.flow_json, plan.get("tm_ev"))      # E115: the Time Machine verdict, record only, never raises
        db.add(pos)
        db.commit()
        db.refresh(pos)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.critical("short live: SELL order %s for %s x%s WAS PLACED but the position row could not be saved (%s). Capital and symbol lock are kept. "
                        "Check Dhan and close it by hand if reconcile does not pick it up.", (plain or {}).get("entry_order_id") or super_id, sym, qty, e)
        try:
            notifier.notify_fire_and_forget(f"🚨 <b>SHORT placed but NOT recorded</b> - {sym} x{qty}\nOrder {(plain or {}).get('entry_order_id') or super_id}. Check Dhan now and cover it by hand if needed.")
        except Exception as ne:  # noqa: BLE001
            logger.warning("short live: alert failed (%s)", ne)
        return None

    try:
        short_entry._log(db, cand, "SHORT_ENTERED", f"SELL {qty} @ {live:.2f} stop {levels.stop_price:.2f} target {levels.target_price:.2f} {'PLAIN_ORDER=' + str(plain.get('entry_order_id')) + ' stop_order=' + str(plain.get('stop_order_id')) if plain else 'SUPER_ORDER=' + str(super_id)}")
        short_entry._stats["entered"] = short_entry._stats.get("entered", 0) + 1
        notifier.notify_fire_and_forget(
            f"📉 <b>SHORT placed</b> - {sym} x{qty}\nEntry ~₹{live:.2f} | Target ₹{levels.target_price:.2f} ({levels.target_pct:.2f}%) | Stop ₹{levels.stop_price:.2f} "
            f"({levels.stop_pct:.2f}%)\nBoard conviction {conv if conv is not None else 'n/a'} | {'Plain order ' + str(plain.get('entry_order_id')) + ' + broker stop ' + str(plain.get('stop_order_id')) + (' (NO broker stop: guard covers by hand)' if not plain.get('protected') else '') if plain else 'Super Order ' + str(super_id)}\nCovered by 14:55 at the latest. Live short cap: {qty_cap()} share(s).")
    except Exception as e:  # noqa: BLE001
        logger.warning("short live: post-entry log/alert for %s failed (%s)", sym, e)
    logger.info("position-stocks SHORT ENTERED %s x%d @ %.2f target=%.2f stop=%.2f order=%s", sym, qty, live, levels.target_price, levels.stop_price, (plain or {}).get("entry_order_id") or super_id)
    return pos


def _recover_ambiguous_sell(db: Session, exc: Exception, security_id: str, qty: int, sym: str) -> Optional[str]:
    """A SELL placement that errored on OUR side (timeout / dropped connection / 5xx) may still exist at Dhan. Look for a young matching SELL before calling
    the entry failed, so a live short never goes untracked. Never raises. Returns the order id or None."""
    try:
        if not dhan_client.is_ambiguous_placement_error(exc):
            return None
        known = set()
        for a, b in db.query(ScalpPosition.dhan_super_order_id, ScalpPosition.dhan_entry_order_id).filter(ScalpPosition.dhan_security_id == str(security_id)).all():
            known.update(x for x in (a, b) if x)
        row = dhan_client.find_recent_buy_order(db, security_id, qty, super_order=True, exclude_ids=known, transaction_type="SELL")
    except Exception as e:  # noqa: BLE001
        logger.warning("short live: %s SELL errored (%s) and the Dhan lookup failed too (%s) - treating as failed", sym, exc, e)
        return None
    if not row:
        return None
    oid = str(row.get("orderId") or row.get("order_id") or row.get("id") or "")
    logger.error("short live: %s SELL errored (%s) BUT Dhan has order %s - RECOVERED, tracking it", sym, exc, oid)
    return oid or None


def _handle_failure(db: Session, cand: Any, sym: str, error_msg: str) -> None:
    """Classify a refused SELL (the Super Order, or the plain SELL of the E114 fallback). Nothing is retried here; a short without a broker stop must never exist."""
    low = error_msg or ""
    try:
        if dhan_client.is_security_intraday_restricted_error(low) or dhan_client.is_circuit_limit_error(low):
            from screening import intraday_eligibility
            intraday_eligibility.record_restriction(db, sym, detail=f"SELL rejection: {low[:200]}")
            code = "SHORT_ORDER_FAILED_RESTRICTED"
        elif dhan_client.is_intraday_cutoff_error(low):
            code = "SHORT_ORDER_FAILED_INTRADAY_CUTOFF"
        elif dhan_client.is_insufficient_funds_error(low):
            entry_pause.pause_all("INSUFFICIENT_FUNDS", config.ENTRY_MARGIN_PAUSE_MINUTES)
            code = "SHORT_ORDER_FAILED_INSUFFICIENT_FUNDS"
        else:
            entry_pause.cooldown_symbol(sym, config.ENTRY_ORDER_FAILED_COOLDOWN_MINUTES, low)
            code = "SHORT_ORDER_FAILED"
    except Exception as e:  # noqa: BLE001
        logger.warning("short live: failure classification for %s failed (%s)", sym, e)
        code = "SHORT_ORDER_FAILED"
    logger.error("short live: SELL for %s refused (%s): %s - no short opened", sym, code, low)
    short_entry._refuse(db, cand, code, low[:400])
    try:
        notifier.notify_fire_and_forget(f"⚠️ Short refused by Dhan - {sym}\n{low[:300]}\nNo short was opened.")
    except Exception as ne:  # noqa: BLE001
        logger.warning("short live: alert failed (%s)", ne)
