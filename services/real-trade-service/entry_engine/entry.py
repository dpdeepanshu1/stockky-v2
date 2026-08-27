"""
entry_engine/entry.py — turns an unconsumed trade_candidates row into
either WAIT (nothing happens) or a bounded, risk-approved DEMO order.

Reuses the SAME target/stop philosophy as api-gateway/buy_sniper.py's ATR
fallback (added earlier this session): scale by the symbol's own ATR% when
available, flat conservative defaults otherwise, never a single "ideal"
number. That module isn't imported directly (different service, different
deploy) — the constants here are the same values, kept in sync by hand
since duplicating a whole scoring module across two microservices for a
handful of numbers isn't worth the coupling.

REAL-mode order placement IS wired (Phase 3, execution/dhan_client.py) —
a risk-approved REAL entry reaches Dhan the same cycle. It is NOT the same
as a confirmed fill: execution/reconcile.py is what turns a broker-accepted
order into an open TradePosition, via Dhan's own order status, never a
simulated price check.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

import config
import models
from audit.logger import log_action
from execution import dhan_client
from market_feed.feed import get_quotes
from notifier import notify_async
from portfolio.portfolio import get_account, open_positions, record_real_order_sent
from risk_engine.engine import AccountState, OrderIntent, RiskVerdict, evaluate as risk_evaluate
from tz_utils import is_market_open_ist

logger = logging.getLogger("real-trade-entry")

# Same conservative bounds as buy_sniper.py's ATR fallback (kept in sync by
# hand — see module docstring).
ATR_STOP_MULTIPLIER = 1.5
ATR_TARGET_MULTIPLIER = 3.0
MIN_STOP_PCT = 2.0
MAX_STOP_PCT = 6.0
FLAT_STOP_PCT = 3.2      # used only when no ATR is available for the symbol
FLAT_TARGET_PCT = 6.5

# A candidate's decision label must be at least this "buy-ish" to even be
# considered — mirrors buy_sniper.py's action gate (BUY NOW / PREPARE TO BUY).
_ACTIONABLE_DECISIONS = {"BUY NOW", "PREPARE TO BUY"}


def _atr_stop_target_pct(atr_pct: float | None) -> tuple[float, float]:
    if atr_pct is None or atr_pct <= 0:
        return FLAT_STOP_PCT, FLAT_TARGET_PCT
    stop_pct = max(MIN_STOP_PCT, min(atr_pct * ATR_STOP_MULTIPLIER, MAX_STOP_PCT))
    target_pct = stop_pct * (ATR_TARGET_MULTIPLIER / ATR_STOP_MULTIPLIER)  # keep ~2:1 reward:risk
    return round(stop_pct, 2), round(target_pct, 2)


def _account_state(db: Session, mode: str, gate_armed: bool) -> AccountState:
    if mode == "REAL":
        # Refreshes cash_available/current_equity from Dhan's live balance
        # before sizing anything — see execution/equity_sync.py's module
        # docstring for why this was the actual reason REAL never approved
        # a BUY (equity stuck at 0, not "balance too small"). Best-effort:
        # if this fails, we fall through and use whatever equity value is
        # already on the account row.
        from execution.equity_sync import sync_real_equity
        sync_real_equity(db)
    account = get_account(db, mode)
    risk = db.query(models.TradeRiskConfig).filter_by(mode=mode).first()
    positions = open_positions(db, mode)
    return AccountState(
        equity=account.current_equity,
        risk_per_trade_pct=risk.risk_per_trade_pct,
        max_daily_loss_pct=risk.max_daily_loss_pct,
        max_concurrent_positions=risk.max_concurrent_positions,
        max_portfolio_risk_pct=risk.max_portfolio_risk_pct,
        stale_data_seconds=risk.stale_data_seconds,
        max_tick_volatility_mult=risk.max_tick_volatility_mult,
        allow_pyramiding=risk.allow_pyramiding,
        realized_pnl_today=account.realized_pnl_today,
        open_position_count=len(positions),
        open_position_symbols={p.symbol for p in positions},
        open_positions_total_risk=sum(
            abs(p.avg_entry_price - (p.current_stop or p.avg_entry_price)) * p.qty_open for p in positions
        ),
        trading_globally_paused=not gate_armed,
        market_is_open=is_market_open_ist(),
    )


async def evaluate_mode(db: Session, mode: str, gate_armed: bool) -> dict:
    """One evaluation cycle for `mode`: pulls unconsumed candidates, prices
    them, risk-checks a proposed entry, and (DEMO only) places the order.
    Always marks every candidate it looked at as consumed=True, whether or
    not it acted — an unconsumed candidate should only ever mean 'the next
    cycle hasn't reached it yet', never 'we silently ignored it'."""
    candidates = (
        db.query(models.TradeCandidate)
        .filter_by(mode=mode, consumed=False)
        .order_by(models.TradeCandidate.received_at.asc())
        .limit(20)  # bound one cycle's work
        .all()
    )
    if not candidates:
        return {"evaluated": 0, "entered": 0, "waited": 0, "rejected": 0}

    symbols = list({c.symbol for c in candidates})
    ticks = await get_quotes(symbols)

    entered = waited = rejected = 0
    for cand in candidates:
        cand.consumed = True
        tick = ticks.get(cand.symbol)
        decision = models.TradeDecision(
            mode=mode, candidate_id=cand.id, symbol=cand.symbol, decision_type="ENTRY",
        )

        if (cand.decision_label or "").upper() not in _ACTIONABLE_DECISIONS:
            decision.action = "WAIT"
            decision.reasoning = f"Source decision '{cand.decision_label}' isn't actionable for entry."
            waited += 1
        elif tick is None:
            decision.action = "WAIT"
            decision.reasoning = "No current price available — market_feed returned nothing."
            waited += 1
        else:
            stop_pct, target_pct = _atr_stop_target_pct(
                (tick.atr / tick.price * 100.0) if tick.atr else None
            )
            entry_price = round(tick.price * (1 + config.ENTRY_ZONE_UPPER_PCT / 100.0 / 2), 2)  # mid-zone, not the ceiling
            stop_price = round(tick.price * (1 - stop_pct / 100.0), 2)
            target_price = round(tick.price * (1 + target_pct / 100.0), 2)

            account_state = _account_state(db, mode, gate_armed)
            per_share_risk = entry_price - stop_price
            if per_share_risk <= 0:
                decision.action = "WAIT"
                decision.reasoning = "Computed stop is not below entry — refusing to size an invalid risk."
                waited += 1
            else:
                max_trade_risk = account_state.equity * (account_state.risk_per_trade_pct / 100.0)
                proposed_qty = max(0, int(max_trade_risk // per_share_risk))
                if proposed_qty <= 0:
                    decision.action = "WAIT"
                    decision.reasoning = "Even 1 share exceeds the per-trade risk cap at this price/stop."
                    waited += 1
                else:
                    intent = OrderIntent(
                        mode=mode, symbol=cand.symbol, side="BUY", qty=proposed_qty,
                        entry_price=entry_price, stop_price=stop_price, target_price=target_price,
                        market_data_timestamp=tick.as_of,
                        recent_atr_pct=(tick.atr / tick.price * 100.0) if tick.atr else None,
                        latest_tick_move_pct=None,  # Phase 3: needs a rolling tick window to compute
                    )
                    result = risk_evaluate(intent, account_state)
                    decision.proposed_qty = result.approved_qty or proposed_qty
                    decision.proposed_price = entry_price
                    decision.proposed_stop = stop_price
                    decision.proposed_target = target_price
                    decision.risk_verdict = result.verdict.value
                    decision.risk_verdict_reason = result.reason

                    db.add(models.TradeRiskEvent(
                        mode=mode, symbol=cand.symbol, check_name=result.check_name,
                        verdict=result.verdict.value, detail=result.reason,
                    ))

                    if result.verdict != RiskVerdict.APPROVED:
                        decision.action = "WAIT"
                        rejected += 1
                    else:
                        decision.action = "ENTER"
                        entered += 1
                        db.add(decision)
                        db.flush()  # need decision.id for the order FK below

                        order = models.TradeOrder(
                            mode=mode, decision_id=decision.id, symbol=cand.symbol, side="BUY",
                            order_type=config.ENTRY_ORDER_TYPE, qty=decision.proposed_qty,
                            limit_price=entry_price,
                            valid_until=datetime.now(timezone.utc) + timedelta(minutes=config.ENTRY_VALIDITY_MINUTES),
                            status="PLACED",
                        )
                        db.add(order)
                        db.flush()
                        db.add(models.TradeOrderEvent(order_id=order.id, event_type="PLACED",
                                                       detail=f"DEMO limit {entry_price}, valid {config.ENTRY_VALIDITY_MINUTES}m"))

                        # For REAL, this is where the order actually reaches
                        # Dhan. A resolution/placement failure here must
                        # WAIT, never silently look like a normal DEMO
                        # PLACED order — the whole point of the gap this
                        # code closes is that "recorded a PLACED row" and
                        # "an order actually exists at the broker" were
                        # conflatable before this.
                        if mode == "REAL":
                            try:
                                security_id = dhan_client.get_security_id(db, cand.symbol)
                                broker_result = dhan_client.place_order(
                                    db, is_armed=gate_armed,
                                    security_id=security_id,
                                    exchange_segment=dhan_client.NSE_EQ_SEGMENT,
                                    transaction_type="BUY",
                                    quantity=decision.proposed_qty,
                                    order_type=config.ENTRY_ORDER_TYPE,
                                    price=entry_price,
                                )
                                dhan_order_id = str(
                                    broker_result.get("orderId") or broker_result.get("order_id") or ""
                                )
                                if not dhan_order_id:
                                    raise RuntimeError(f"Dhan accepted the order but returned no order id: {broker_result}")
                                record_real_order_sent(db, order, dhan_order_id)
                                await notify_async(
                                    f"📤 *BUY sent (auto)* — {cand.symbol}\n"
                                    f"{decision.proposed_qty} @ ₹{entry_price:.2f} · "
                                    f"stop ₹{stop_price:.2f} · target ₹{target_price:.2f}\n"
                                    f"Awaiting broker fill confirmation."
                                )
                            except Exception as e:  # noqa: BLE001 — must never let a broker/API error crash the cycle
                                logger.error("REAL order placement failed for %s: %s", cand.symbol, e)
                                order.status = "REJECTED"
                                db.add(models.TradeOrderEvent(order_id=order.id, event_type="REJECTED",
                                                               detail=f"Dhan placement failed: {e}"))
                                decision.action = "WAIT"
                                decision.reasoning = f"Risk-approved but Dhan placement failed: {e}"
                                entered -= 1
                                waited += 1
                                db.commit()
                                await notify_async(
                                    f"⚠️ *Auto BUY rejected by Dhan* — {cand.symbol}\n{str(e)[:300]}"
                                )

        db.add(decision)  # safe even if already added above (ENTER path) — SQLAlchemy add() is idempotent

    db.commit()
    log_action(db, actor="system", action="ENTRY_CYCLE", mode=mode,
               detail=f"evaluated={len(candidates)} entered={entered} waited={waited} rejected={rejected}")
    return {"evaluated": len(candidates), "entered": entered, "waited": waited, "rejected": rejected}


async def check_pending_fills(db: Session, mode: str) -> int:
    """DEMO-only. Every PLACED order gets checked against the latest tick
    each cycle — this is what actually turns a placed limit order into a
    filled position; entry evaluate_mode() only ever gets an order to
    PLACED, it never fills it directly. Returns count filled."""
    if mode != "DEMO":
        return 0  # REAL fills come from Dhan/reconciliation, Phase 3 — never simulated
    from portfolio.portfolio import try_fill_entry

    pending = db.query(models.TradeOrder).filter_by(mode=mode, status="PLACED", side="BUY").all()
    if not pending:
        return 0
    symbols = list({o.symbol for o in pending})
    ticks = await get_quotes(symbols)

    filled = 0
    for order in pending:
        tick = ticks.get(order.symbol)
        if tick is None:
            continue
        decision = db.query(models.TradeDecision).filter_by(id=order.decision_id).first()
        stop_price = decision.proposed_stop if decision else order.limit_price * (1 - FLAT_STOP_PCT / 100.0)
        target_price = decision.proposed_target if decision else order.limit_price * (1 + FLAT_TARGET_PCT / 100.0)
        if try_fill_entry(db, order, tick, stop_price, target_price):
            filled += 1
    return filled


async def expire_stale_orders(db: Session, mode: str) -> int:
    """Cancels any PLACED order whose valid_until has passed — the
    'never chase price' half of decision 1. Returns count expired."""
    now = datetime.now(timezone.utc)
    stale = (
        db.query(models.TradeOrder)
        .filter(models.TradeOrder.mode == mode, models.TradeOrder.status == "PLACED",
                models.TradeOrder.valid_until.isnot(None), models.TradeOrder.valid_until < now)
        .all()
    )
    for order in stale:
        order.status = "EXPIRED"
        order.updated_at = now
        db.add(models.TradeOrderEvent(order_id=order.id, event_type="EXPIRED",
                                       detail="Entry window closed unfilled — no chase, re-evaluate next cycle."))
    if stale:
        db.commit()
        log_action(db, actor="system", action="ORDERS_EXPIRED", mode=mode, detail=f"count={len(stale)}")
    return len(stale)
