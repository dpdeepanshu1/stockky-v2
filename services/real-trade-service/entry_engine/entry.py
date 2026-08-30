"""
entry_engine/entry.py

ADAPTIVE THRESHOLD SYSTEM (improvement over previous fixed-snapshot approach):
═══════════════════════════════════════════════════════════════════════════════
The Aug-2026 patch hard-coded ENTRY_REGIME_MIN_SCORE=38. This was correct
for that market but freezes silently. This version uses adaptive_thresholds.py
to compute the regime gate dynamically from the 20th percentile of trailing
90-day market scores:
  - Bull market (scores 60-80): gate auto-loosens to ~55
  - Correction (scores 25-45):  gate tightens to ~28-32
  - Falls back to static 38 if < 30 days of history

Every WAIT reasoning string now includes the threshold age so the dashboard
shows: "market_score=31 < gate=38 (set 2026-08-28, 12d ago)" — making
frozen judgments visible at decision time, not just at code-review time.

All other entry logic (drift, R:R, conviction sizing, Dhan placement) unchanged.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
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
import pipeline_status as pstat

# §6 — corporate-action clamp for ATR inputs (return_sanity.py in service root)
try:
    from return_sanity import clamp_for_atr as _clamp_for_atr
except ImportError:
    def _clamp_for_atr(x):
        return None if x is None or abs(x) > 30.0 else x

logger = logging.getLogger("real-trade-entry")

# ── Stop / target constants ───────────────────────────────────────────────────
ATR_STOP_MULTIPLIER   = 1.5
ATR_TARGET_MULTIPLIER = 3.0
MIN_STOP_PCT  = 2.0
MAX_STOP_PCT  = 6.0
FLAT_STOP_PCT   = 3.2
FLAT_TARGET_PCT = 6.5

# ── Entry quality gates (env-overridable, adaptive layer applies on top) ──────
MIN_REWARD_RISK_RATIO = float(os.getenv("ENTRY_MIN_REWARD_RISK", "2.0"))
MAX_ENTRY_DRIFT_ATR   = float(os.getenv("ENTRY_MAX_DRIFT_ATR", "0.75"))

# Static fallback for regime gate — adaptive layer overrides this at runtime.
REGIME_MIN_SCORE_STATIC = int(os.getenv("ENTRY_REGIME_MIN_SCORE", "38"))

# Conviction sizing
CONVICTION_MIDPOINT  = float(os.getenv("ENTRY_CONVICTION_MIDPOINT", "65.0"))
CONVICTION_MAX_SCALE = float(os.getenv("ENTRY_CONVICTION_MAX_SCALE", "0.25"))

_ACTIONABLE_DECISIONS = {"BUY NOW", "PREPARE TO BUY"}

# ── Adaptive regime cache (2-minute TTL to avoid DB hit every candidate) ─────
_regime_cache: dict = {"score": None, "threshold": None, "source": None, "ts": 0.0}
_REGIME_TTL_S = 120.0


async def _get_market_regime(db: Session) -> tuple[bool, int, int, str]:
    """
    Returns (regime_ok, market_score, threshold_used, threshold_source).

    NEW vs original:
      - Persists the fetched score to MarketRegimeHistory for adaptive learning.
      - Gets threshold from adaptive_thresholds.adaptive_regime_threshold(db)
        instead of the static REGIME_MIN_SCORE_STATIC.
      - threshold_source is 'adaptive_Nd_p20' or 'static' — included in
        WAIT reasoning so the dashboard shows threshold age at every block.
    """
    import time as _t
    now = _t.time()
    if (
        _regime_cache["score"] is not None
        and (now - _regime_cache["ts"]) < _REGIME_TTL_S
    ):
        score     = _regime_cache["score"]
        threshold = _regime_cache["threshold"]
        source    = _regime_cache["source"]
        return score >= threshold, score, threshold, source

    score     = 50
    threshold = REGIME_MIN_SCORE_STATIC
    source    = "static"

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{config.API_GATEWAY_URL}/market/indices",
                params={"force_refresh": "false"},
                timeout=8.0,
            )
            if r.status_code == 200:
                data  = r.json()
                score = int(data.get("market_score") or 50)
    except Exception as e:
        logger.debug("regime fetch failed (fail-open): %s", e)

    # Persist score for adaptive learning (non-blocking, best-effort)
    try:
        from adaptive_thresholds import record_market_score, adaptive_regime_threshold
        record_market_score(db, score)
        threshold, source = adaptive_regime_threshold(db)
    except Exception as e:
        logger.debug("adaptive threshold fetch failed (using static): %s", e)

    _regime_cache.update({"score": score, "threshold": threshold, "source": source, "ts": _t.time()})
    return score >= threshold, score, threshold, source


# ── Stop / target helpers ─────────────────────────────────────────────────────

def _atr_stop_target_pct(atr_pct: Optional[float]) -> tuple[float, float]:
    if atr_pct is None or atr_pct <= 0:
        return FLAT_STOP_PCT, FLAT_TARGET_PCT
    stop_pct   = max(MIN_STOP_PCT, min(atr_pct * ATR_STOP_MULTIPLIER, MAX_STOP_PCT))
    target_pct = stop_pct * (ATR_TARGET_MULTIPLIER / ATR_STOP_MULTIPLIER)
    return round(stop_pct, 2), round(target_pct, 2)


def _reward_risk_ratio(entry: float, stop: float, target: float) -> float:
    risk   = entry - stop
    reward = target - entry
    if risk <= 0:
        return 0.0
    return round(reward / risk, 2)


def _conviction_adjusted_risk_pct(base_risk_pct: float, conviction_score: Optional[float]) -> float:
    """Scale risk ±25% based on conviction. Score 100→+25%, 65→0%, 0→-25%."""
    if conviction_score is None:
        return base_risk_pct
    score = max(0.0, min(100.0, float(conviction_score)))
    if score >= CONVICTION_MIDPOINT:
        delta = (score - CONVICTION_MIDPOINT) / max(100.0 - CONVICTION_MIDPOINT, 1.0)
    else:
        delta = (score - CONVICTION_MIDPOINT) / max(CONVICTION_MIDPOINT, 1.0)
    adjust = max(-CONVICTION_MAX_SCALE, min(CONVICTION_MAX_SCALE, delta * CONVICTION_MAX_SCALE))
    return round(base_risk_pct * (1.0 + adjust), 4)


def _entry_drift_ok(
    current_price: float,
    signal_price: Optional[float],
    atr_pct: Optional[float],
) -> tuple[bool, str]:
    if signal_price is None or signal_price <= 0 or current_price <= 0:
        return True, ""
    drift_pct     = (current_price - signal_price) / signal_price * 100
    one_atr_pct   = atr_pct if (atr_pct and atr_pct > 0) else 3.0
    max_drift_pct = one_atr_pct * MAX_ENTRY_DRIFT_ATR
    if drift_pct > max_drift_pct:
        return False, (
            f"Price ₹{current_price:.2f} ran +{drift_pct:.1f}% above signal "
            f"₹{signal_price:.2f} (limit {max_drift_pct:.1f}% = {MAX_ENTRY_DRIFT_ATR}×ATR). Chasing — skip."
        )
    if drift_pct < -max_drift_pct:
        return False, (
            f"Price ₹{current_price:.2f} fell {drift_pct:.1f}% below signal "
            f"₹{signal_price:.2f} (limit {max_drift_pct:.1f}%). Move may be done — re-evaluate next cycle."
        )
    return True, ""


def _account_state(db: Session, mode: str, gate_armed: bool, reserved_cash: float = 0.0) -> AccountState:
    if mode == "REAL":
        from execution.equity_sync import sync_real_equity
        sync_real_equity(db)
    account   = get_account(db, mode)
    risk      = db.query(models.TradeRiskConfig).filter_by(mode=mode).first()
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
            abs(p.avg_entry_price - (p.current_stop or p.avg_entry_price)) * p.qty_open
            for p in positions
        ),
        trading_globally_paused=not gate_armed,
        market_is_open=is_market_open_ist(),
        cash_available=max(0.0, account.cash_available - reserved_cash),
    )


# ── Main evaluation cycle ─────────────────────────────────────────────────────

async def evaluate_mode(db: Session, mode: str, gate_armed: bool) -> dict:
    candidates = (
        db.query(models.TradeCandidate)
        .filter_by(mode=mode, consumed=False)
        .order_by(models.TradeCandidate.received_at.asc())
        .limit(20)
        .all()
    )
    if not candidates:
        return {"evaluated": 0, "entered": 0, "waited": 0, "rejected": 0, "entry_details": []}

    # Fetch adaptive regime once per cycle (not per candidate)
    regime_ok    = True
    market_score = 50
    threshold    = REGIME_MIN_SCORE_STATIC
    threshold_src = "static"
    if mode == "REAL":
        regime_ok, market_score, threshold, threshold_src = await _get_market_regime(db)
        if not regime_ok:
            # Get age annotation for the threshold
            try:
                from adaptive_thresholds import threshold_age_note
                age_note = threshold_age_note("ENTRY_REGIME_MIN_SCORE")
            except Exception:
                age_note = ""
            logger.info(
                "entry_engine: REAL regime WEAK score=%d < gate=%d (%s) %s — BUYs blocked.",
                market_score, threshold, threshold_src, age_note,
            )

    symbols = list({c.symbol for c in candidates})
    ticks   = await get_quotes(symbols)

    entered = waited = rejected = 0
    entry_details: list[dict] = []
    reserved_cash = 0.0

    for idx, cand in enumerate(candidates):
        try:
            pstat.set_symbol_progress(mode, cand.symbol, idx, len(candidates))
        except Exception:
            pass

        cand.consumed = True
        tick     = ticks.get(cand.symbol)
        decision = models.TradeDecision(
            mode=mode, candidate_id=cand.id, symbol=cand.symbol, decision_type="ENTRY",
        )

        def _wait(reason: str) -> None:
            nonlocal waited
            decision.action    = "WAIT"
            decision.reasoning = reason
            waited += 1

        # ── Gate 1: actionable decision label ─────────────────────────────────
        if (cand.decision_label or "").upper() not in _ACTIONABLE_DECISIONS:
            _wait(f"Source decision '{cand.decision_label}' is not actionable for entry.")
            db.add(decision)
            entry_details.append({"symbol": cand.symbol, "action": "WAIT",
                                   "reasoning": decision.reasoning, "risk_verdict": None})
            continue

        # ── Gate 2: price available ───────────────────────────────────────────
        if tick is None:
            _wait("No current price available — market_feed returned nothing.")
            db.add(decision)
            entry_details.append({"symbol": cand.symbol, "action": "WAIT",
                                   "reasoning": decision.reasoning, "risk_verdict": None})
            continue

        # §6 — clamp ATR: exclude corporate-action day jumps > 30%
        raw_atr_pct = (tick.atr / tick.price * 100.0) if tick.atr else None
        atr_pct = _clamp_for_atr(raw_atr_pct) if raw_atr_pct is not None else None

        # ── Gate 3: adaptive market regime (REAL only) ─────────────────────────
        if mode == "REAL" and not regime_ok:
            try:
                from adaptive_thresholds import threshold_age_note
                age_note = threshold_age_note("ENTRY_REGIME_MIN_SCORE")
            except Exception:
                age_note = ""
            _wait(
                f"Market regime score {market_score} < adaptive gate {threshold} "
                f"({threshold_src}) {age_note}. "
                "Nifty regime is weak — deferring new BUY entries until recovery."
            )
            db.add(decision)
            entry_details.append({"symbol": cand.symbol, "action": "WAIT",
                                   "reasoning": decision.reasoning, "risk_verdict": None})
            continue

        # ── Gate 4: entry drift ───────────────────────────────────────────────
        drift_ok, drift_reason = _entry_drift_ok(tick.price, cand.signal_price, atr_pct)
        if not drift_ok:
            _wait(drift_reason)
            db.add(decision)
            entry_details.append({"symbol": cand.symbol, "action": "WAIT",
                                   "reasoning": drift_reason, "risk_verdict": None})
            continue

        # ── Compute prices ────────────────────────────────────────────────────
        stop_pct, target_pct = _atr_stop_target_pct(atr_pct)
        entry_price  = round(tick.price * (1 + config.ENTRY_ZONE_UPPER_PCT / 100.0 / 2), 2)
        stop_price   = round(tick.price * (1 - stop_pct   / 100.0), 2)
        target_price = round(tick.price * (1 + target_pct / 100.0), 2)
        per_share_risk = entry_price - stop_price

        if per_share_risk <= 0:
            _wait("Computed stop is not below entry — invalid risk setup.")
            db.add(decision)
            entry_details.append({"symbol": cand.symbol, "action": "WAIT",
                                   "reasoning": decision.reasoning, "risk_verdict": None})
            continue

        # ── Gate 5: reward:risk floor ─────────────────────────────────────────
        rr = _reward_risk_ratio(entry_price, stop_price, target_price)
        if rr < MIN_REWARD_RISK_RATIO:
            _wait(
                f"R:R {rr:.2f}:1 below floor {MIN_REWARD_RISK_RATIO:.1f}:1 — "
                "setup does not offer enough reward for the risk. Skip."
            )
            db.add(decision)
            entry_details.append({"symbol": cand.symbol, "action": "WAIT",
                                   "reasoning": decision.reasoning, "risk_verdict": None})
            continue

        # ── Conviction-adjusted sizing ─────────────────────────────────────────
        account_state  = _account_state(db, mode, gate_armed, reserved_cash)
        adj_risk_pct   = _conviction_adjusted_risk_pct(
            account_state.risk_per_trade_pct, cand.conviction_score
        )
        max_trade_risk = account_state.equity * (adj_risk_pct / 100.0)
        proposed_qty   = max(0, int(max_trade_risk // per_share_risk))

        if proposed_qty <= 0:
            _wait(
                f"Even 1 share exceeds conviction-adjusted risk cap "
                f"({adj_risk_pct:.2f}% of equity = ₹{max_trade_risk:.2f})."
            )
            db.add(decision)
            entry_details.append({"symbol": cand.symbol, "action": "WAIT",
                                   "reasoning": decision.reasoning, "risk_verdict": None})
            continue

        # ── Risk engine ───────────────────────────────────────────────────────
        # §5 — pass avg_traded_value for liquidity floor check in risk_engine
        # Try to get from tick, then from candidate raw payload, fail-open to None
        avg_traded_value = None
        try:
            vol = getattr(tick, 'volume', None) or 0
            if vol and tick.price:
                avg_traded_value = float(vol) * float(tick.price)
        except Exception:
            pass

        intent = OrderIntent(
            mode=mode, symbol=cand.symbol, side="BUY", qty=proposed_qty,
            entry_price=entry_price, stop_price=stop_price, target_price=target_price,
            market_data_timestamp=tick.as_of,
            recent_atr_pct=atr_pct,
            latest_tick_move_pct=None,
            avg_traded_value=avg_traded_value,
        )
        result = risk_evaluate(intent, account_state)

        decision.proposed_qty        = result.approved_qty or proposed_qty
        decision.proposed_price      = entry_price
        decision.proposed_stop       = stop_price
        decision.proposed_target     = target_price
        decision.risk_verdict        = result.verdict.value
        decision.risk_verdict_reason = result.reason

        db.add(models.TradeRiskEvent(
            mode=mode, symbol=cand.symbol, check_name=result.check_name,
            verdict=result.verdict.value, detail=result.reason,
        ))

        if result.verdict != RiskVerdict.APPROVED:
            decision.action = "WAIT"
            rejected += 1
            db.add(decision)
            entry_details.append({"symbol": cand.symbol, "action": "WAIT",
                                   "reasoning": result.reason, "risk_verdict": result.verdict.value})
            continue

        # ── Approved — create order ────────────────────────────────────────────
        decision.action = "ENTER"
        entered        += 1
        reserved_cash  += decision.proposed_qty * entry_price
        db.add(decision)
        db.flush()

        order = models.TradeOrder(
            mode=mode, decision_id=decision.id, symbol=cand.symbol, side="BUY",
            order_type=config.ENTRY_ORDER_TYPE, qty=decision.proposed_qty,
            limit_price=entry_price,
            valid_until=datetime.now(timezone.utc) + timedelta(minutes=config.ENTRY_VALIDITY_MINUTES),
            status="PLACED",
        )
        db.add(order)
        db.flush()
        db.add(models.TradeOrderEvent(
            order_id=order.id, event_type="PLACED",
            detail=(
                f"Limit ₹{entry_price:.2f} | stop ₹{stop_price:.2f} | "
                f"target ₹{target_price:.2f} | R:R {rr:.2f} | "
                f"conviction {cand.conviction_score} | adj_risk {adj_risk_pct:.2f}% | "
                f"regime_score {market_score} (gate={threshold},{threshold_src}) | "
                f"valid {config.ENTRY_VALIDITY_MINUTES}m"
            ),
        ))

        # ── REAL: place at Dhan ────────────────────────────────────────────────
        if mode == "REAL":
            try:
                security_id   = dhan_client.get_security_id(db, cand.symbol)
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
                    raise RuntimeError(f"Dhan returned no order id: {broker_result}")
                record_real_order_sent(db, order, dhan_order_id)
                await notify_async(
                    f"📤 *BUY sent (auto)* — {cand.symbol}\n"
                    f"{decision.proposed_qty} @ ₹{entry_price:.2f} "
                    f"| stop ₹{stop_price:.2f} | target ₹{target_price:.2f}\n"
                    f"R:R {rr:.2f} | conviction {cand.conviction_score} "
                    f"| market score {market_score} (gate={threshold},{threshold_src})\n"
                    f"Awaiting broker fill confirmation."
                )
            except Exception as e:
                logger.error("REAL order placement failed for %s: %s", cand.symbol, e)
                order.status = "REJECTED"
                db.add(models.TradeOrderEvent(
                    order_id=order.id, event_type="REJECTED",
                    detail=f"Dhan placement failed: {e}",
                ))
                decision.action = "WAIT"
                if dhan_client.is_invalid_ip_error(str(e)):
                    decision.reasoning = (
                        "Risk-approved but blocked: Dhan rejected — outbound IP not whitelisted. "
                        "Auto-paused REAL. See GET /dhan/network-check."
                    )
                    entered       -= 1
                    waited        += 1
                    reserved_cash -= decision.proposed_qty * entry_price
                    entry_details.append({
                        "symbol": cand.symbol, "action": decision.action,
                        "reasoning": decision.reasoning, "risk_verdict": decision.risk_verdict,
                    })
                    db.commit()
                    from auth.dhan_credentials import disarm_on_invalid_ip
                    just_disarmed = disarm_on_invalid_ip(db, mode, str(e))
                    if just_disarmed:
                        await notify_async(
                            "🚨 *REAL trading auto-paused* — Dhan rejecting orders (IP not whitelisted). "
                            "Check GET /dhan/network-check for the IP to whitelist."
                        )
                    return {
                        "evaluated": len(candidates), "entered": entered,
                        "waited": waited, "rejected": rejected,
                        "auto_disarmed": "invalid_ip", "entry_details": entry_details,
                    }
                decision.reasoning = f"Risk-approved but Dhan placement failed: {e}"
                entered       -= 1
                waited        += 1
                reserved_cash -= decision.proposed_qty * entry_price  # release reserved capital
                db.commit()
                await notify_async(
                    f"⚠️ *Auto BUY rejected by Dhan* — {cand.symbol}\n{str(e)[:300]}"
                )

        db.add(decision)
        entry_details.append({
            "symbol": cand.symbol, "action": decision.action,
            "reasoning": decision.reasoning, "risk_verdict": decision.risk_verdict,
        })

    db.commit()
    log_action(db, actor="system", action="ENTRY_CYCLE", mode=mode,
               detail=(
                   f"evaluated={len(candidates)} entered={entered} waited={waited} "
                   f"rejected={rejected} market_score={market_score} "
                   f"regime_gate={threshold}({threshold_src})"
               ))
    return {
        "evaluated": len(candidates), "entered": entered,
        "waited": waited, "rejected": rejected,
        "entry_details": entry_details,
        "regime": {"score": market_score, "gate": threshold, "source": threshold_src},
    }


# ── DEMO pending fill simulation ──────────────────────────────────────────────

async def check_pending_fills(db: Session, mode: str) -> int:
    if mode != "DEMO":
        return 0
    from portfolio.portfolio import try_fill_entry
    pending = db.query(models.TradeOrder).filter_by(mode=mode, status="PLACED", side="BUY").all()
    if not pending:
        return 0
    symbols = list({o.symbol for o in pending})
    ticks   = await get_quotes(symbols)
    filled  = 0
    for order in pending:
        tick = ticks.get(order.symbol)
        if tick is None:
            continue
        dec    = db.query(models.TradeDecision).filter_by(id=order.decision_id).first()
        stop_p = dec.proposed_stop   if dec else order.limit_price * (1 - FLAT_STOP_PCT   / 100.0)
        tgt_p  = dec.proposed_target if dec else order.limit_price * (1 + FLAT_TARGET_PCT / 100.0)
        if try_fill_entry(db, order, tick, stop_p, tgt_p):
            filled += 1
    return filled


async def expire_stale_orders(db: Session, mode: str) -> int:
    now   = datetime.now(timezone.utc)
    stale = (
        db.query(models.TradeOrder)
        .filter(
            models.TradeOrder.mode        == mode,
            models.TradeOrder.status      == "PLACED",
            models.TradeOrder.valid_until.isnot(None),
            models.TradeOrder.valid_until  < now,
        )
        .all()
    )
    for order in stale:
        order.status     = "EXPIRED"
        order.updated_at = now
        db.add(models.TradeOrderEvent(
            order_id=order.id, event_type="EXPIRED",
            detail="Entry window closed unfilled — no chase.",
        ))
    if stale:
        db.commit()
        log_action(db, actor="system", action="ORDERS_EXPIRED", mode=mode,
                   detail=f"count={len(stale)}")
    return len(stale)
