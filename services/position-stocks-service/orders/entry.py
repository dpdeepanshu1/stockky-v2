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
from capital import ledger, shared_order_budget
from execution import dhan_client
from models import ScalpCandidateLog, ScalpGateState, ScalpPosition
from orders.adaptive import AdaptiveLevels, compute as compute_levels
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
    return row


def _count_open_positions(db: Session) -> int:
    return db.query(ScalpPosition).filter_by(status="OPEN").count()


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

    # Compute adaptive levels
    levels: AdaptiveLevels = compute_levels(candidate.pct_change, candidate.current_ltp)

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
        logger.error("position-stocks entry: order placement failed for %s: %s", candidate.symbol, e)
        ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
        _log_candidate(db, candidate, "SKIPPED", f"ORDER_FAILED:{error_msg}", quality=quality)
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
    return pos
