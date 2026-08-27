"""
manual_engine.py — Manual Execution Gateway.

Turns a user-submitted trade ticket (BUY/SELL, qty, order type, price,
stop, target, product) into the SAME risk-checked, audited order path
entry_engine/exit_engine already use for automatic candidates. This is
the "MANUAL" source feeding the one shared candidate/risk/execution
backbone — per the 2026-08-27 plan: one execution authority (risk engine
-> execution gate -> simulator/Dhan), three sources (MANUAL / AUTO /
EXIT), never three separate trading code paths.

Two-step flow, called from main.py's /manual-order/{mode}/preview and
/manual-order/{mode}/confirm routes — never trust the frontend to only
call confirm after a clean preview:

  1. evaluate_manual_order(..., confirm=False) — "Review Order" screen.
     Risk-evaluates a hypothetical intent (or, for SELL, just prices the
     estimated P&L against an existing position). Writes NOTHING to the
     DB. Safe to call on every keystroke/qty-slider change.

  2. evaluate_manual_order(..., confirm=True) — "Confirm BUY/SELL".
     Re-derives EVERYTHING from the current DB state again (current tick,
     current account equity, current open positions) rather than trusting
     any number the client echoes back from an earlier preview call —
     price, funds, and open-position count can all have changed in the
     seconds between the two calls. Only then does it write the
     TradeDecision/TradeOrder rows and actually send the order (REAL:
     Dhan: DEMO: an immediate simulated fill attempt via the exact same
     portfolio.try_fill_entry() the automatic entry cycle uses).

BUY only opens/adds to a position — long-only, matching portfolio.py's
own module note (no shorting in this phase). SELL only reduces an
EXISTING open position; a SELL with no matching position is refused, not
silently turned into a naked short. SELL deliberately does NOT go through
risk_evaluate()'s global-pause check — closing a position must never be
blocked by "not armed", exactly like the existing manual-close endpoint
and every automatic exit already behave (see risk_engine's own check #3
comment: a positions-count-style check must never apply to an exit).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

import config
import models
from audit.logger import log_action
from execution import dhan_client
from exit_engine.exit import _send_real_sell
from market_feed.feed import get_quotes
from portfolio.portfolio import close_position, get_account, open_positions, record_real_order_sent, try_fill_entry
from risk_engine.engine import AccountState, OrderIntent, RiskVerdict, evaluate as risk_evaluate

logger = logging.getLogger("real-trade-manual")

# Same flat fallback used by entry_engine.entry when a manual ticket
# doesn't specify its own stop/target — kept as a literal, small copy
# rather than importing entry.py's private constants, so this module
# never breaks if entry_engine's own defaults are retuned independently.
_FALLBACK_STOP_PCT = 3.2
_FALLBACK_TARGET_PCT = 6.5


def _account_state(db: Session, mode: str, gate_armed: bool) -> AccountState:
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
        market_is_open=True,  # same Phase-3 TODO as entry_engine — wire to market_feed's real market-hours check
    )


def _resolve_position(db: Session, mode: str, symbol: str, position_id: Optional[int]) -> Optional[models.TradePosition]:
    q = db.query(models.TradePosition).filter(
        models.TradePosition.mode == mode,
        models.TradePosition.status.in_(("OPEN", "PARTIALLY_CLOSED")),
    )
    if position_id is not None:
        q = q.filter(models.TradePosition.id == position_id)
    else:
        q = q.filter(models.TradePosition.symbol == symbol)
    return q.first()


async def evaluate_manual_order(
    db: Session, mode: str, gate_armed: bool, req, *, confirm: bool, admin: Optional[str] = None,
) -> dict:
    symbol = (req.symbol or "").upper().strip().replace(".NS", "").replace(".BO", "")
    side = (req.side or "").upper()
    order_type = (req.order_type or "LIMIT").upper()
    product_type = (req.product_type or "CNC").upper()

    if not symbol:
        return {"ok": False, "reason": "invalid_request", "detail": "symbol is required."}
    if side not in ("BUY", "SELL"):
        return {"ok": False, "reason": "invalid_request", "detail": "side must be BUY or SELL."}
    if not req.qty or req.qty <= 0:
        return {"ok": False, "reason": "invalid_request", "detail": "qty must be positive."}
    if order_type not in ("LIMIT", "MARKET"):
        return {"ok": False, "reason": "invalid_request", "detail": "order_type must be LIMIT or MARKET."}

    ticks = await get_quotes([symbol])
    tick = ticks.get(symbol)
    if tick is None:
        return {"ok": False, "reason": "no_price", "detail": f"No current price available for {symbol} — try again shortly."}

    reference_price = req.limit_price if (order_type == "LIMIT" and req.limit_price) else tick.price

    # ── BUY: new/added position, full risk-engine path ─────────────────
    if side == "BUY":
        stop_price = req.stop_price or round(reference_price * (1 - _FALLBACK_STOP_PCT / 100.0), 2)
        target_price = req.target_price or round(reference_price * (1 + _FALLBACK_TARGET_PCT / 100.0), 2)
        if stop_price >= reference_price:
            return {"ok": False, "reason": "invalid_stop", "detail": "Stop price must be below entry price."}

        account_state = _account_state(db, mode, gate_armed)
        intent = OrderIntent(
            mode=mode, symbol=symbol, side="BUY", qty=req.qty,
            entry_price=reference_price, stop_price=stop_price, target_price=target_price,
            market_data_timestamp=tick.as_of,
            recent_atr_pct=(tick.atr / tick.price * 100.0) if tick.atr else None,
        )
        result = risk_evaluate(intent, account_state)
        approved_qty = result.approved_qty if result.approved_qty is not None else (
            req.qty if result.verdict == RiskVerdict.APPROVED else 0
        )
        per_share_risk = max(0.0, reference_price - stop_price)
        preview = {
            "ok": result.verdict == RiskVerdict.APPROVED and approved_qty > 0,
            "mode": mode, "symbol": symbol, "side": "BUY",
            "order_type": order_type, "product_type": product_type,
            "entry_price": reference_price, "stop_price": stop_price, "target_price": target_price,
            "qty_requested": req.qty, "approved_qty": approved_qty,
            "risk_amount": round(per_share_risk * approved_qty, 2),
            "estimated_value": round(reference_price * approved_qty, 2),
            "risk_reward": round((target_price - reference_price) / per_share_risk, 2) if per_share_risk > 0 else None,
            "verdict": result.verdict.value, "check_name": result.check_name, "reason": result.reason,
        }
        if not confirm:
            return preview
        if not preview["ok"]:
            log_action(db, actor=admin or "admin", action="MANUAL_ORDER_REJECTED", mode=mode,
                       detail=f"{symbol} BUY x{req.qty}: {result.check_name} — {result.reason}")
            return preview

        decision = models.TradeDecision(
            mode=mode, symbol=symbol, decision_type="ENTRY", action="ENTER",
            reasoning="Manual order (Stockky Trade ticket)",
            proposed_qty=approved_qty, proposed_price=reference_price,
            proposed_stop=stop_price, proposed_target=target_price,
            risk_verdict=result.verdict.value, risk_verdict_reason=result.reason,
        )
        db.add(decision)
        db.flush()

        order = models.TradeOrder(
            mode=mode, decision_id=decision.id, symbol=symbol, side="BUY",
            order_type=order_type, qty=approved_qty, limit_price=reference_price,
            valid_until=datetime.now(timezone.utc) + timedelta(minutes=config.ENTRY_VALIDITY_MINUTES),
            status="PLACED", execution_source="MANUAL",
            confirmed_by=admin, confirmed_at=datetime.now(timezone.utc) if admin else None,
        )
        db.add(order)
        db.flush()
        db.add(models.TradeOrderEvent(order_id=order.id, event_type="PLACED",
                                       detail=f"Manual {order_type} {reference_price}, sent by {admin or 'demo-user'}"))
        db.commit()
        log_action(db, actor=admin or "admin", action="MANUAL_ORDER_CONFIRMED", mode=mode,
                   detail=f"{symbol} BUY x{approved_qty} @ {reference_price} ({order_type}/{product_type})")

        preview["order_id"] = order.id
        preview["decision_id"] = decision.id

        if mode == "DEMO":
            # MARKET tickets fill at the current tick unconditionally —
            # try_fill_entry's own gate (tick.price <= limit_price) is
            # already satisfied because limit_price WAS set to tick.price
            # above for a MARKET order, so this is a plain reuse of the
            # exact same fill path the automatic entry cycle uses, not a
            # parallel implementation.
            filled = try_fill_entry(db, order, tick, stop_price, target_price)
            preview["status"] = "FILLED" if filled else "PLACED"
            preview["filled"] = filled
        else:
            try:
                security_id = dhan_client.get_security_id(db, symbol)
                broker_result = dhan_client.place_order(
                    db, is_armed=gate_armed, security_id=security_id,
                    exchange_segment=dhan_client.NSE_EQ_SEGMENT, transaction_type="BUY",
                    quantity=approved_qty, order_type=order_type, price=reference_price,
                    product_type=product_type,
                )
                dhan_order_id = str(broker_result.get("orderId") or broker_result.get("order_id") or "")
                if not dhan_order_id:
                    raise RuntimeError(f"Dhan accepted the order but returned no order id: {broker_result}")
                record_real_order_sent(db, order, dhan_order_id)
                log_action(db, actor=admin or "admin", action="MANUAL_BUY_SENT", mode=mode,
                           detail=f"{symbol} x{approved_qty} -> Dhan order {dhan_order_id}")
                preview["status"] = "SENT_TO_BROKER"
                preview["dhan_order_id"] = dhan_order_id
            except Exception as e:  # noqa: BLE001 — must surface as a clean rejection, never a 500
                logger.error("Manual REAL BUY failed for %s: %s", symbol, e)
                order.status = "REJECTED"
                db.add(models.TradeOrderEvent(order_id=order.id, event_type="REJECTED",
                                               detail=f"Dhan placement failed: {e}"))
                db.commit()
                log_action(db, actor=admin or "admin", action="MANUAL_ORDER_REJECTED", mode=mode,
                           detail=f"{symbol} BUY x{approved_qty}: Dhan placement failed: {e}")
                preview["ok"] = False
                preview["status"] = "REJECTED"
                preview["reason"] = "dhan_error"
                preview["detail"] = str(e)
        return preview

    # ── SELL: reduce an existing position — no global-pause gate, matches
    #    the existing manual-close endpoint's "exits are always allowed"
    #    policy (and risk_engine's own comment on why side==SELL never
    #    runs the positions-count style checks). ─────────────────────────
    position = _resolve_position(db, mode, symbol, req.position_id)
    if position is None:
        return {"ok": False, "reason": "no_position", "detail": f"No open {mode} position in {symbol} to sell."}

    qty = min(req.qty, position.qty_open)
    if qty <= 0:
        return {"ok": False, "reason": "invalid_qty", "detail": "qty must be positive."}

    estimated_pnl = round((reference_price - position.avg_entry_price) * qty, 2)
    preview = {
        "ok": True, "mode": mode, "symbol": symbol, "side": "SELL", "position_id": position.id,
        "qty_available": position.qty_open, "qty_requested": req.qty, "approved_qty": qty,
        "exit_price_estimate": reference_price, "estimated_pnl": estimated_pnl,
    }
    if not confirm:
        return preview

    log_action(db, actor=admin or "admin", action="MANUAL_ORDER_CONFIRMED", mode=mode,
               detail=f"{symbol} SELL x{qty} (position {position.id})")

    if mode == "DEMO":
        pnl = close_position(db, position, tick, qty, "manual_sell")
        preview["pnl"] = pnl
        preview["status"] = "CLOSED" if position.status == "CLOSED" else "PARTIALLY_CLOSED"
    else:
        full = qty >= position.qty_open
        sent = _send_real_sell(db, position, qty, "manual_sell", full=full,
                                execution_source="MANUAL", confirmed_by=admin)
        if not sent:
            log_action(db, actor=admin or "admin", action="MANUAL_ORDER_REJECTED", mode=mode,
                       detail=f"{symbol} SELL x{qty}: Dhan rejected the order — see server logs.")
            preview["ok"] = False
            preview["status"] = "REJECTED"
            preview["detail"] = "Dhan rejected the manual sell order — see server logs."
        else:
            log_action(db, actor=admin or "admin", action="MANUAL_SELL_SENT", mode=mode,
                       detail=f"{symbol} x{qty} sent to Dhan (position {position.id})")
            preview["status"] = "SENT_TO_BROKER"
    return preview
