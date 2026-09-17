"""
orders/entry.py — position entry logic.

Wires together: candidate → adaptive levels → capital sizing →
Dhan Super Order (or plain order fallback) → DB record.

Safety checks (in order):
  1. is_armed guard
  2. Max concurrent positions check
  3. Capital reserve (also enforces daily loss kill switch)
  4. Dhan security_id resolution
  5. Quantity computation (position_value / current_ltp, min 1)
  6. FIRST_LIVE_ORDER_MIN_QTY_OVERRIDE → force qty=1 on first-ever order
  7. Super Order placement (or plain MARKET fallback)
  8. DB record write
  9. Candidate log write
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

import config
import notifier
from capital import ledger, shared_order_budget, shared_symbol_lock
from execution import dhan_client
from models import ScalpCandidateLog, ScalpGateState, ScalpPosition
from orders.adaptive import AdaptiveLevels, compute as compute_levels
from screening import intraday_eligibility
from screening.engine import Candidate
from screening.quality_gate import QualitySignal
from tz_utils import ist_today_str

logger = logging.getLogger("position-stocks-entry")


def _get_gate_state(db: Session) -> ScalpGateState:
    row = db.query(ScalpGateState).filter_by(mode="REAL").first()
    if row is None:
        row = ScalpGateState(mode="REAL")
        db.add(row)
        db.commit()
        db.refresh(row)
    # BUG FIX (session13, found via live testing): lazy reset of
    # gate.daily_loss_kill_switch_tripped — see main.py's
    # _maybe_lazy_reset_gate_kill_switch() for the full story. This is the
    # copy that actually matters here: it's what the early-exit check just
    # below reads.
    if row.daily_loss_kill_switch_tripped and row.daily_loss_kill_switch_tripped_date != ist_today_str():
        row.daily_loss_kill_switch_tripped = False
        row.daily_loss_kill_switch_tripped_date = None
        db.commit()
        logger.info("gate: lazy daily reset applied to gate.daily_loss_kill_switch_tripped")
    return row


def _count_open_positions(db: Session) -> int:
    # BUG FIX (audit follow-up to session41b/42): only counted status="OPEN",
    # so an EXIT_LEGS_REJECTED position (still holding real capital and real
    # exposure — its exit legs were rejected, not its entry) was invisible to
    # the MAX_CONCURRENT_SCALP_POSITIONS gate below. That let the service
    # open MORE concurrent positions than the configured cap whenever a
    # stuck position existed, since it didn't count against the limit.
    return (
        db.query(ScalpPosition)
        .filter(ScalpPosition.status.in_(("OPEN", "EXIT_LEGS_REJECTED")))
        .count()
    )


def _log_candidate(
    db: Session,
    candidate: Candidate,
    decision: str,
    reason: str,
    composite_score: Optional[float] = None,
    quality: Optional[QualitySignal] = None,
) -> None:
    log = ScalpCandidateLog(
        symbol=candidate.symbol,
        window_source=candidate.window_label,
        pct_change=candidate.pct_change,
        composite_score=composite_score or candidate.composite_score,
        decision=decision,
        reason=reason,
        fundamental_score=quality.fundamental_score if quality else None,
        technical_score=quality.technical_score if quality else None,
        market_cap_cr=quality.market_cap_cr if quality else None,
        has_positive_catalyst=quality.has_positive_catalyst if quality else None,
    )
    db.add(log)
    db.commit()


def log_quality_reject(db: Session, candidate: Candidate, quality: QualitySignal, reason: str) -> None:
    """Record a candidate skipped by the quality gate BEFORE attempt_entry
    was even called (main.py's trading loop checks quality for the top-N
    candidates first) — keeps the audit trail (ScalpCandidateLog) complete
    for candidates that never reached the capital/Dhan checks inside
    attempt_entry."""
    _log_candidate(db, candidate, "SKIPPED", f"QUALITY_GATE:{reason}", quality=quality)


def attempt_entry(
    db: Session,
    candidate: Candidate,
    quality: Optional[QualitySignal] = None,
) -> Optional[ScalpPosition]:
    """Try to enter a position for the given candidate.
    Returns the ScalpPosition if entered, None otherwise (also logs why).
    `quality` (added session 6) is the best-effort fundamental/technical/
    news signal from screening/quality_gate.py, already checked by the
    caller — passed through here purely so it's recorded on the
    ScalpCandidateLog row alongside the entry/skip decision, for a full
    audit trail of what was known about a symbol at decision time."""

    gate = _get_gate_state(db)

    if not gate.is_armed:
        _log_candidate(db, candidate, "SKIPPED", "SERVICE_NOT_ARMED", quality=quality)
        return None

    if gate.daily_loss_kill_switch_tripped:
        _log_candidate(db, candidate, "SKIPPED", "DAILY_LOSS_KILL_SWITCH", quality=quality)
        return None

    # Order budget guard
    today = ist_today_str()
    if gate.orders_placed_today_date == today and gate.orders_placed_today >= config.DAILY_ORDER_BUDGET:
        _log_candidate(db, candidate, "SKIPPED", f"ORDER_BUDGET_EXHAUSTED:{gate.orders_placed_today}", quality=quality)
        return None

    # Max concurrent positions
    open_count = _count_open_positions(db)
    if open_count >= config.MAX_CONCURRENT_SCALP_POSITIONS:
        _log_candidate(db, candidate, "SKIPPED", f"MAX_POSITIONS:{open_count}", quality=quality)
        return None

    # AUDIT FIX (session60): cross-service symbol lock — this service and
    # real-trade-service share one Dhan account, which holds a single
    # consolidated position per symbol with no concept of which service's
    # shares are whose. Checked here, before any capital is reserved,
    # because it's a cheap DB read and a duplicate-symbol buy should be
    # skipped as early as possible, not discovered after capital's already
    # committed. See capital/shared_symbol_lock.py for full context
    # (confirmed cause of the AEGISVOPAK broker order-type mismatch).
    if not shared_symbol_lock.try_claim(db, candidate.symbol):
        _log_candidate(db, candidate, "SKIPPED", "SYMBOL_HELD_BY_OTHER_SERVICE", quality=quality)
        return None

    # Compute adaptive levels — pass symbol so range-aware adjustment can
    # read today's intraday high/low from the live tick buffer (session41b).
    levels: AdaptiveLevels = compute_levels(
        candidate.pct_change, candidate.current_ltp, symbol=candidate.symbol
    )

    # Reserve capital (also checks kill switch again in the ledger)
    position_value = ledger.reserve_capital(db, adaptive_stop_pct=levels.stop_pct)
    if position_value is None:
        _log_candidate(db, candidate, "SKIPPED", "INSUFFICIENT_CAPITAL", quality=quality)
        return None

    # Resolve Dhan security_id
    try:
        security_id = dhan_client.get_security_id(db, candidate.symbol)
    except dhan_client.SecurityNotResolvedError as e:
        ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
        shared_symbol_lock.release(db, candidate.symbol)
        _log_candidate(db, candidate, "SKIPPED", f"SECURITY_NOT_FOUND:{e}", quality=quality)
        return None

    # Quantity
    raw_qty = int(position_value / candidate.current_ltp)
    quantity = max(1, raw_qty)

    is_first = not gate.first_live_order_done
    if is_first and config.FIRST_LIVE_ORDER_MIN_QTY_OVERRIDE:
        logger.warning(
            "position-stocks: FIRST LIVE SUPER ORDER — forcing qty=1 (was %d) "
            "to observe Dhan's actual response. Disable FIRST_LIVE_ORDER_MIN_QTY_OVERRIDE "
            "once you've confirmed a clean fill.", quantity,
        )
        quantity = 1

    # BUG FIX (this session): quantity is floored to at least 1 share above
    # (and may be forced to exactly 1 by the first-live-order override) —
    # but reserve_capital() above only ever deducted the risk-SIZED
    # `position_value` from the ledger, which can be SMALLER than what
    # `quantity` shares actually cost (a small pool and/or a high-priced
    # stock). Placing the real order anyway would spend more real rupees
    # than the ledger ever accounted for, silently overstating
    # available_capital afterwards — and since this pool is a
    # software-enforced half of one real, shared Dhan account, that
    # unaccounted overspend can eat into real-trade-service's half without
    # either ledger reflecting it. Reserve the real shortfall (if any)
    # before placing the order; skip the entry if even that isn't
    # available rather than place an order the ledger can't actually back.
    actual_cost = quantity * candidate.current_ltp
    if actual_cost > position_value:
        shortfall = actual_cost - position_value
        if not ledger.reserve_additional(db, shortfall):
            ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
            shared_symbol_lock.release(db, candidate.symbol)
            _log_candidate(
                db, candidate, "SKIPPED",
                f"INSUFFICIENT_CAPITAL_FOR_MIN_QTY:shortfall={shortfall:.2f}",
                quality=quality,
            )
            return None
        position_value = actual_cost

    # Shared cross-service Dhan account-wide order-rate guard (tracking doc
    # §3.8) — checked here, right before the real Dhan call, not earlier:
    # everything above this point (max positions, capital, security
    # resolution) can still reject a candidate for reasons that have
    # nothing to do with order-rate, and none of those should count against
    # the shared budget. Separate from this service's OWN order budget
    # checked above — Dhan's real account-wide cap is shared with
    # real-trade-service too. Fails open on any error (see
    # shared_order_budget.py's docstring).
    if not shared_order_budget.check_and_reserve(db):
        ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
        shared_symbol_lock.release(db, candidate.symbol)
        _log_candidate(db, candidate, "SKIPPED", "SHARED_ORDER_BUDGET_EXHAUSTED", quality=quality)
        return None

    # Place the order
    dhan_super_order_id = None
    error_msg = None
    try:
        if config.USE_SUPER_ORDER:
            result = dhan_client.place_super_order(
                db,
                is_armed=gate.is_armed,
                security_id=security_id,
                exchange_segment=config.SCALP_EXCHANGE_SEGMENT,
                transaction_type="BUY",
                quantity=quantity,
                order_type="MARKET",
                price=candidate.current_ltp,   # entry reference price
                target_price=levels.target_price,
                stop_loss_price=levels.stop_price,
                trailing_jump=0.0,
                product_type=config.SCALP_PRODUCT_TYPE,
                tag="SCALP",
            )
            dhan_super_order_id = str(result.get("orderId") or result.get("id") or "")
        else:
            # Plain MARKET order fallback
            result = dhan_client.place_order(
                db,
                is_armed=gate.is_armed,
                security_id=security_id,
                exchange_segment=config.SCALP_EXCHANGE_SEGMENT,
                transaction_type="BUY",
                quantity=quantity,
                order_type="MARKET",
                price=0.0,
                product_type=config.SCALP_PRODUCT_TYPE,
                tag="SCALP",
            )
    except Exception as e:
        error_msg = str(e)
        # AUDIT FIX (2026-09-15): classify the BUY-side rejection too — if
        # Dhan rejects the entry itself as "not allowed to be traded in
        # Intraday", record the restriction immediately so this symbol is
        # filtered out of future cycles before capital is ever reserved for
        # it. Previously, a BUY rejection just fell into the generic
        # ORDER_FAILED path with no diagnosis and no learning.
        if dhan_client.is_security_intraday_restricted_error(error_msg):
            logger.error(
                "position-stocks entry: BUY rejected — %s is INTRADAY_RESTRICTED "
                "(T2T/ASM/GSM). Recording restriction to exclude from future cycles. "
                "Error: %s", candidate.symbol, error_msg,
            )
            try:
                intraday_eligibility.record_restriction(
                    db, candidate.symbol,
                    detail=f"BUY rejection: {error_msg[:200]}",
                )
            except Exception as rec_e:
                logger.warning(
                    "position-stocks entry: could not record restriction for %s: %s",
                    candidate.symbol, rec_e,
                )
            ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
            _log_candidate(db, candidate, "SKIPPED", f"ORDER_FAILED_INTRADAY_RESTRICTED:{error_msg}", quality=quality)
        elif dhan_client.is_intraday_cutoff_error(error_msg):
            logger.warning(
                "position-stocks entry: BUY rejected — INTRADAY_CUTOFF: exchange window "
                "closed for today. Will retry tomorrow. Error: %s", error_msg,
            )
            ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
            _log_candidate(db, candidate, "SKIPPED", f"ORDER_FAILED_INTRADAY_CUTOFF:{error_msg}", quality=quality)
        elif dhan_client.is_insufficient_funds_error(error_msg):
            logger.error(
                "position-stocks entry: BUY rejected — INSUFFICIENT_FUNDS. "
                "Error: %s", error_msg,
            )
            ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
            _log_candidate(db, candidate, "SKIPPED", f"ORDER_FAILED_INSUFFICIENT_FUNDS:{error_msg}", quality=quality)
        elif dhan_client.is_circuit_limit_error(error_msg):
            # 2026-09-15 fix (session41b): "Rate Not Within Ckt Limit X To Y"
            # — the stock has already hit its upper circuit; our BUY price
            # was outside the allowed band.  Retrying at the same price can
            # NEVER succeed this session.  Record it as intraday-restricted
            # (same mechanism as T2T/ASM stocks) so it's excluded from all
            # future cycles today — a stock at upper circuit has no intraday
            # exit room even if the BUY did fill.
            logger.error(
                "position-stocks entry: BUY rejected — CIRCUIT_LIMIT (stock at upper "
                "circuit, no intraday exit room). Recording restriction for today. "
                "Error: %s", error_msg,
            )
            ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
            try:
                from screening import intraday_eligibility
                intraday_eligibility.record_restriction(db, candidate.symbol)
                logger.info(
                    "position-stocks entry: %s added to intraday-restricted set (circuit limit)",
                    candidate.symbol,
                )
            except Exception as rec_e:
                logger.warning(
                    "position-stocks entry: failed to record circuit-limit restriction for %s: %s",
                    candidate.symbol, rec_e,
                )
            _log_candidate(db, candidate, "SKIPPED", f"ORDER_FAILED_CIRCUIT_LIMIT:{error_msg}", quality=quality)
        else:
            logger.error("position-stocks entry: order placement failed for %s: %s", candidate.symbol, e)
            ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
            _log_candidate(db, candidate, "SKIPPED", f"ORDER_FAILED:{error_msg}", quality=quality)
        # AUDIT FIX (session60): every branch above releases capital on a
        # failed BUY — the symbol claim taken above (before adaptive
        # levels were even computed) must be released here too, or a
        # failed order permanently locks this symbol out for this service
        # (real-trade-service is unaffected either way, since it never
        # held the lock).
        shared_symbol_lock.release(db, candidate.symbol)
        return None

    # Mark first-live-order done
    if is_first:
        gate.first_live_order_done = True

    # Order budget increment
    if gate.orders_placed_today_date != today:
        gate.orders_placed_today = 0
        gate.orders_placed_today_date = today
    gate.orders_placed_today += 1
    db.commit()

    # DB record
    pos = ScalpPosition(
        symbol=candidate.symbol,
        dhan_security_id=security_id,
        window_source=candidate.window_label,
        status="OPEN",
        entry_price=candidate.current_ltp,
        quantity=quantity,
        target_price=levels.target_price,
        stop_price=levels.stop_price,
        adaptive_target_pct=levels.target_pct,
        adaptive_stop_pct=levels.stop_pct,
        dhan_super_order_id=dhan_super_order_id,
        # AUDIT FIX (session22 cont'd): dhan_entry_order_id was declared on
        # the model but never written anywhere, so it was always NULL even
        # though the frontend/API now expose it. Per Dhan's super-order
        # response shape (see reconcile.py's docstring), the top-level
        # orderId of a super order IS the ENTRY_LEG's order id — there is
        # no separate id Dhan issues for it — so it's set equal to
        # dhan_super_order_id here rather than left to default to NULL.
        dhan_entry_order_id=dhan_super_order_id,
        capital_risked=position_value,
        is_first_live_order=is_first,
        opened_at=datetime.now(timezone.utc),
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)

    _log_candidate(db, candidate, "ENTERED",
                   f"SUPER_ORDER={dhan_super_order_id or 'plain_order'}",
                   composite_score=candidate.composite_score,
                   quality=quality)
    logger.info(
        "position-stocks ENTERED %s x%d @ ₹%.2f target=₹%.2f stop=₹%.2f "
        "(window=%s score=%.3f super_order=%s)",
        candidate.symbol, quantity, candidate.current_ltp,
        levels.target_price, levels.stop_price,
        candidate.window_label, candidate.composite_score,
        dhan_super_order_id or "N/A",
    )
    # BUG FIX (this session — "no notification got for position stocks
    # order on telegram"): notifier.py has existed since session41 but was
    # ONLY ever called for CRITICAL failures (broker mismatches, dead EOD
    # sells) — a completely normal, successful BUY never notified anyone,
    # unlike real-trade-service's entry_engine.py which notifies on every
    # BUY it sends. Mirrors that message shape (see entry_engine.py's
    # "📤 BUY sent (auto)" notify_async call), best-effort/non-blocking
    # same as every other notifier.py call in this codebase.
    notifier.notify_sync(
        f"📤 <b>BUY placed</b> — {candidate.symbol} x{quantity}\n"
        f"Entry ₹{candidate.current_ltp:.2f} | Target ₹{levels.target_price:.2f} "
        f"({levels.target_pct:.2f}%) | Stop ₹{levels.stop_price:.2f} ({levels.stop_pct:.2f}%)\n"
        f"Window {candidate.window_label} | Capital ₹{position_value:,.2f} "
        f"| Super Order {dhan_super_order_id or 'N/A'}"
    )
    return pos


class ManualEntryRejected(Exception):
    """Raised by attempt_manual_entry() for any rejection the caller (a
    live admin request, not a background scan) should see as an explicit
    error response rather than a silently-logged skip — unlike
    attempt_entry() above (called from the unattended scan loop, where a
    None return + a ScalpCandidateLog row is the right contract)."""


def attempt_manual_entry(
    db: Session,
    symbol: str,
    current_ltp: float,
    quantity: Optional[int] = None,
) -> ScalpPosition:
    """Manual BUY — an admin picks the symbol (and optionally the exact
    quantity) directly from the dashboard, bypassing the screener/quality
    gate entirely (this IS the "manual control to buy" feature — the
    screener only ever proposes candidates automatically; there was no
    path for an admin to just buy something they're watching).

    Deliberately reuses every safety gate attempt_entry() enforces for an
    automatic candidate — armed check, daily-loss kill switch, per-day
    order budget, max-concurrent-positions cap, adaptive target/stop
    sizing, the shared cross-service Dhan order-rate guard, and the same
    Super Order (or plain MARKET fallback) placement path — so a manual
    BUY is exactly as safe as an automatic one, just skipping the
    window-scan/quality-gate SELECTION step, not any of the RISK checks.

    quantity: if given, sizes the position to exactly this many shares
    (reserving quantity * current_ltp from the ledger) instead of the
    automatic risk-based sizing attempt_entry() uses. Still subject to the
    same available_capital check — an admin can't manually buy more than
    the ledger has free, same as the automatic path can't.

    Raises ManualEntryRejected with a human-readable reason on any
    rejection (armed/kill-switch/budget/capital/security-id/Dhan) instead
    of returning None — this is a live admin action expecting an explicit
    error, not a background scan skip. Returns the created ScalpPosition
    on success.
    """
    from screening.engine import Candidate

    gate = _get_gate_state(db)

    if not gate.is_armed:
        raise ManualEntryRejected("Service is not armed — arm it before placing a manual BUY.")

    if gate.daily_loss_kill_switch_tripped:
        raise ManualEntryRejected("Daily loss kill switch is tripped — no new entries today.")

    today = ist_today_str()
    if gate.orders_placed_today_date == today and gate.orders_placed_today >= config.DAILY_ORDER_BUDGET:
        raise ManualEntryRejected(f"Daily order budget exhausted ({gate.orders_placed_today}).")

    open_count = _count_open_positions(db)
    if open_count >= config.MAX_CONCURRENT_SCALP_POSITIONS:
        raise ManualEntryRejected(
            f"Max concurrent positions reached ({open_count}/{config.MAX_CONCURRENT_SCALP_POSITIONS})."
        )

    if current_ltp <= 0:
        raise ManualEntryRejected(f"No valid live price for {symbol}.")

    # AUDIT FIX (session60): same cross-service symbol lock as attempt_entry()
    # — a manual BUY is exactly as capable of colliding with a
    # real-trade-service holding as an automatic one, and this is the
    # admin-facing path, so it gets an explicit rejection rather than a
    # silent skip.
    symbol = symbol.strip().upper()
    if not shared_symbol_lock.try_claim(db, symbol):
        raise ManualEntryRejected(
            f"{symbol} is already held by real-trade-service on the shared Dhan "
            "account — buying it here too would create a duplicate broker "
            "position. Close it on the other service first if you want to "
            "re-enter it here."
        )

    # Adaptive levels — pct_change=0.0 since this is a manual pick, not a
    # window-scan signal; compute() falls back to ATR-proxy from the tick
    # buffer when available (the normal case for any actively-traded NSE
    # symbol this service's WS feed has already ticked), and only falls
    # back further to a pct_change-derived stop when the buffer is too
    # thin — see adaptive.py's compute() docstring.
    levels: AdaptiveLevels = compute_levels(0.0, current_ltp, symbol=symbol)

    if quantity is not None:
        if quantity <= 0:
            raise ManualEntryRejected("quantity must be a positive integer.")
        exact_cost = quantity * current_ltp
        if not ledger.reserve_additional(db, exact_cost):
            shared_symbol_lock.release(db, symbol)
            raise ManualEntryRejected(
                f"Insufficient capital: need ₹{exact_cost:,.2f} for {quantity} shares of {symbol}."
            )
        position_value = exact_cost
    else:
        position_value = ledger.reserve_capital(db, adaptive_stop_pct=levels.stop_pct)
        if position_value is None:
            shared_symbol_lock.release(db, symbol)
            raise ManualEntryRejected("Insufficient available capital (or daily-loss kill switch tripped).")
        quantity = max(1, int(position_value / current_ltp))
        actual_cost = quantity * current_ltp
        if actual_cost > position_value:
            shortfall = actual_cost - position_value
            if not ledger.reserve_additional(db, shortfall):
                ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
                shared_symbol_lock.release(db, symbol)
                raise ManualEntryRejected(
                    f"Insufficient capital to cover the minimum 1-share order "
                    f"(shortfall ₹{shortfall:,.2f})."
                )
            position_value = actual_cost

    try:
        security_id = dhan_client.get_security_id(db, symbol)
    except dhan_client.SecurityNotResolvedError as e:
        ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
        shared_symbol_lock.release(db, symbol)
        raise ManualEntryRejected(f"Could not resolve a Dhan security id for {symbol}: {e}")

    if not shared_order_budget.check_and_reserve(db):
        ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
        shared_symbol_lock.release(db, symbol)
        raise ManualEntryRejected("Shared cross-service Dhan order-rate budget exhausted for today.")

    is_first = not gate.first_live_order_done
    dhan_super_order_id = None
    try:
        if config.USE_SUPER_ORDER:
            result = dhan_client.place_super_order(
                db,
                is_armed=gate.is_armed,
                security_id=security_id,
                exchange_segment=config.SCALP_EXCHANGE_SEGMENT,
                transaction_type="BUY",
                quantity=quantity,
                order_type="MARKET",
                price=current_ltp,
                target_price=levels.target_price,
                stop_loss_price=levels.stop_price,
                trailing_jump=0.0,
                product_type=config.SCALP_PRODUCT_TYPE,
                tag="MANUAL",
            )
            dhan_super_order_id = str(result.get("orderId") or result.get("id") or "")
        else:
            result = dhan_client.place_order(
                db,
                is_armed=gate.is_armed,
                security_id=security_id,
                exchange_segment=config.SCALP_EXCHANGE_SEGMENT,
                transaction_type="BUY",
                quantity=quantity,
                order_type="MARKET",
                price=0.0,
                product_type=config.SCALP_PRODUCT_TYPE,
                tag="MANUAL",
            )
    except Exception as e:
        ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
        shared_symbol_lock.release(db, symbol)
        db.add(ScalpCandidateLog(
            symbol=symbol, window_source="MANUAL", pct_change=0.0,
            composite_score=None, decision="SKIPPED",
            reason=f"MANUAL_ORDER_FAILED:{e}",
        ))
        db.commit()
        raise ManualEntryRejected(f"Dhan rejected the manual BUY: {e}")

    if is_first:
        gate.first_live_order_done = True
    if gate.orders_placed_today_date != today:
        gate.orders_placed_today = 0
        gate.orders_placed_today_date = today
    gate.orders_placed_today += 1
    db.commit()

    pos = ScalpPosition(
        symbol=symbol,
        dhan_security_id=security_id,
        window_source="MANUAL",
        status="OPEN",
        entry_price=current_ltp,
        quantity=quantity,
        target_price=levels.target_price,
        stop_price=levels.stop_price,
        adaptive_target_pct=levels.target_pct,
        adaptive_stop_pct=levels.stop_pct,
        dhan_super_order_id=dhan_super_order_id,
        dhan_entry_order_id=dhan_super_order_id,
        capital_risked=position_value,
        is_first_live_order=is_first,
        opened_at=datetime.now(timezone.utc),
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)

    db.add(ScalpCandidateLog(
        symbol=symbol, window_source="MANUAL", pct_change=0.0,
        composite_score=None, decision="ENTERED",
        reason=f"MANUAL_BUY:SUPER_ORDER={dhan_super_order_id or 'plain_order'}",
    ))
    db.commit()

    logger.info(
        "position-stocks MANUAL BUY %s x%d @ ₹%.2f target=₹%.2f stop=₹%.2f super_order=%s",
        symbol, quantity, current_ltp, levels.target_price, levels.stop_price,
        dhan_super_order_id or "N/A",
    )
    notifier.notify_sync(
        f"📤 <b>Manual BUY placed</b> — {symbol} x{quantity}\n"
        f"Entry ₹{current_ltp:.2f} | Target ₹{levels.target_price:.2f} "
        f"({levels.target_pct:.2f}%) | Stop ₹{levels.stop_price:.2f} ({levels.stop_pct:.2f}%)\n"
        f"Capital ₹{position_value:,.2f} | Super Order {dhan_super_order_id or 'N/A'}"
    )
    return pos
