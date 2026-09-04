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
from market_feed.feed import get_quotes, get_preview_quotes, MARKET_DATA_URL
from notifier import notify_async
from portfolio.portfolio import get_account, open_positions, record_real_order_sent
from risk_engine.engine import AccountState, OrderIntent, RiskVerdict, evaluate as risk_evaluate
from tz_utils import is_market_open_ist
import pipeline_status as pstat
from execution.dhan_client import round_to_tick

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

# BUG FIX (31-Aug-2026): this set used to be {"BUY NOW", "PREPARE TO BUY"}
# only — a straight copy-paste of candidate_engine/candidates.py's own
# _ACTIONABLE_DECISIONS at the time. When the volume-shock momentum track
# was added there (see candidates.py's _refresh_volume_shock_candidates —
# "Option A (Issue 1 fix)"), it started inserting TradeCandidate rows with
# decision_label "VOLUME_SHOCK" / "VOLUME_SHOCK_HIGH_CONVICTION" /
# "VOLUME_SHOCK_UPPER_CIRCUIT", but this file's copy of the set was never
# updated to match. Every volume-shock candidate — the entire point of that
# track — was therefore permanently stuck on WAIT with "Source decision
# 'VOLUME_SHOCK' is not actionable for entry.", visible in the dashboard's
# Live Cycle Status (candidates found, 0 entered, all WAIT) even though
# candidate_engine was finding and inserting them correctly. There is
# nothing else gating these labels anywhere else in the entry path, so this
# was a straight dead-end for that whole feature, not a deliberate filter.
_ACTIONABLE_DECISIONS = {
    "BUY NOW", "PREPARE TO BUY",
    "VOLUME_SHOCK", "VOLUME_SHOCK_HIGH_CONVICTION", "VOLUME_SHOCK_UPPER_CIRCUIT",
}

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
            # BUG FIX (2026-09-01): was abs(avg_entry_price - current_stop) —
            # for a position whose stop has been moved to/above entry (see
            # exit_engine/exit.py's breakeven-stop and age-aware ATR-trail
            # logic, both of which explicitly ratchet current_stop upward
            # once a trade is in profit), avg_entry_price - current_stop is
            # negative: hitting that stop locks in a GAIN, not a loss. abs()
            # turned that guaranteed-profit distance into a positive "risk"
            # figure and added it to open_positions_total_risk, which feeds
            # directly into risk_engine's max_portfolio_risk cap just below
            # (§6: prospective_total = open_positions_total_risk + order_risk).
            # That overstated the portfolio's real downside exposure for
            # every position that had moved favorably, and could reject a
            # genuinely safe new entry because already-de-risked winners were
            # still being counted at their full original risk. Clamping to 0
            # means a position whose stop is at/above entry contributes
            # nothing to the portfolio risk cap, matching what
            # max_portfolio_risk is actually meant to measure: capital that
            # would be lost if every open stop got hit right now.
            max(0.0, p.avg_entry_price - (p.current_stop or p.avg_entry_price)) * p.qty_open
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

    # Preview (last-close) prices ONLY for symbols with no live tick, so a WAIT
    # can still show Waiting-at / Stop / Target instead of blank '—' columns and
    # can name why the live quote is missing. These are non-tradeable: the ENTER
    # path below never runs on a preview (it requires a real live tick).
    missing_syms = [s for s in symbols if ticks.get(s) is None]
    preview_ticks: dict = {}
    if missing_syms:
        try:
            preview_ticks = await get_preview_quotes(missing_syms)
        except Exception as e:
            logger.debug("preview quote fetch failed (non-fatal): %s", e)
            preview_ticks = {}

    # ── Regime-gate override (2026-08-31) ───────────────────────────────────
    # When the market-wide gate is blocking everything, still let the
    # handful of strongest candidates this cycle through it — see the
    # config.py docstring on ENTRY_REGIME_OVERRIDE_TOP_N for why. Picking the
    # override set BEFORE the main loop (rather than "first N seen") means it
    # is actually the highest-conviction names, not just whichever happened
    # to be queued first.
    override_ids: set[int] = set()
    if mode == "REAL" and not regime_ok and config.ENTRY_REGIME_OVERRIDE_TOP_N > 0:
        override_eligible = [
            c for c in candidates
            if (c.decision_label or "").upper() in _ACTIONABLE_DECISIONS and ticks.get(c.symbol) is not None
        ]
        override_eligible.sort(key=lambda c: (c.conviction_score or 0), reverse=True)
        override_ids = {c.id for c in override_eligible[:config.ENTRY_REGIME_OVERRIDE_TOP_N]}

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
            # No live tick. Try a preview (last-close) price so the dashboard
            # shows Waiting-at / Stop / Target instead of blank columns, and so
            # the WAIT explains itself (which market-data host we tried). This
            # is informational ONLY — with no live tick we never reach ENTER, so
            # no order is ever priced off this preview.
            pv = preview_ticks.get(cand.symbol)
            if pv is not None:
                _pstop_pct, _ptgt_pct = _atr_stop_target_pct(
                    _clamp_for_atr(pv.atr / pv.price * 100.0) if pv.atr else None
                )
                _p_entry = round_to_tick(pv.price * (1 + config.ENTRY_ZONE_UPPER_PCT / 100.0 / 2))
                # 2026-09-01 fix: stop/target anchored to _p_entry (the actual
                # price this preview implies you'd pay), not the raw pv.price —
                # see the matching fix + comment on the live-tick path below
                # for why anchoring to pre-premium price systematically breaks R:R.
                _p_stop  = round(_p_entry * (1 - _pstop_pct / 100.0), 2)
                _p_tgt   = round(_p_entry * (1 + _ptgt_pct / 100.0), 2)
                _wait(
                    f"No live quote (showing preview from last close ₹{pv.price:.2f}). "
                    f"Waiting for a fresh tick from market-data before entry — "
                    f"if this persists the market-data service ({MARKET_DATA_URL}) "
                    "may be cold-starting, rate-limited, or MARKET_DATA_URL is misconfigured."
                )
                decision.proposed_price  = _p_entry
                decision.proposed_stop   = _p_stop
                decision.proposed_target = _p_tgt
            else:
                _wait(
                    "No current price available — market_feed returned nothing "
                    f"(tried live_quotes + {MARKET_DATA_URL}/quote and last-close). "
                    "The market-data service may be asleep (Render cold start), "
                    "rate-limited, or MARKET_DATA_URL may be unset/misconfigured."
                )
            db.add(decision)
            entry_details.append({"symbol": cand.symbol, "action": "WAIT",
                                   "reasoning": decision.reasoning,
                                   "risk_verdict": None})
            continue

        # §6 — clamp ATR: exclude corporate-action day jumps > 30%
        raw_atr_pct = (tick.atr / tick.price * 100.0) if tick.atr else None
        atr_pct = _clamp_for_atr(raw_atr_pct) if raw_atr_pct is not None else None

        # ── Compute prices (moved ahead of gates 3/4 — a tick is all this
        # needs, so every WAIT from here on can carry a preview entry/stop/
        # target instead of leaving the dashboard's "Waiting at" / "Stop
        # loss" columns blank. This is informational only: it does not
        # affect qty, risk sizing, or order placement, which still happen
        # only after gates 3-5 pass below.) ───────────────────────────────
        stop_pct, target_pct = _atr_stop_target_pct(atr_pct)
        # 2026-09-01 fix: entry_price is the actual LIMIT price sent to Dhan —
        # must be a valid NSE tick (₹0.05 multiple) or the broker rejects the
        # order outright. See execution/dhan_client.py's TICK_SIZE comment —
        # this was previously a plain 2dp round, which is off-tick ~80% of
        # the time and was silently killing risk-approved BUYs at the
        # "Dhan placement failed" step below with no distinct signal that
        # tick size was the cause. stop_price/target_price stay plain
        # rounding — they're internal trigger levels compared against a
        # continuous LTP, never sent to the broker as an order price.
        entry_price  = round_to_tick(tick.price * (1 + config.ENTRY_ZONE_UPPER_PCT / 100.0 / 2))
        # 2026-09-01 fix (R:R-floor false-reject bug): stop_price/target_price
        # used to be computed off tick.price (raw current LTP) while
        # entry_price is tick.price PLUS the ENTRY_ZONE_UPPER_PCT/2 premium
        # (added by the tick-size fix so the limit order lands on a valid
        # ₹0.05 grid point). Reward/risk was then measured between entry_price
        # and those tick.price-anchored levels — mixing two different base
        # prices — which silently added the premium to risk and subtracted it
        # from reward on every single candidate, every time, regardless of
        # symbol. For the ATR-based case (stop=1.5x, target=3x ATR — a clean,
        # by-design 2.0:1) and the flat fallback (3.2%/6.5% — 2.03:1), that
        # fixed erosion (2 x ENTRY_ZONE_UPPER_PCT/2 = 0.5% of price, split
        # between the two legs) was enough to push literally every setup
        # below MIN_REWARD_RISK_RATIO=2.0 — e.g. FLAT case computed to exactly
        # 1.81:1 every time, matching the dashboard's identical R:R across
        # unrelated symbols (ENGINERS, GRAPHITE, REDINGTON, VTL all showing
        # 1.81:1) that admin flagged as "why isn't it picking any stock."
        # Anchoring stop_price/target_price to entry_price instead — the
        # price this trade would actually be filled at — makes reward/risk
        # exactly target_pct/stop_pct again (2.0:1 ATR case, 2.03:1 flat
        # case), independent of the entry premium, and is also the more
        # correct number: your real risk/reward is relative to what you'd
        # actually pay, not to a stale pre-premium tick.
        stop_price   = round(entry_price * (1 - stop_pct   / 100.0), 2)
        target_price = round(entry_price * (1 + target_pct / 100.0), 2)
        per_share_risk = entry_price - stop_price

        def _wait_with_preview(reason: str) -> None:
            _wait(reason)
            decision.proposed_price  = entry_price
            decision.proposed_stop   = stop_price
            decision.proposed_target = target_price

        # ── Gate 3: adaptive market regime (REAL only) ─────────────────────────
        is_regime_override = mode == "REAL" and not regime_ok and cand.id in override_ids
        if mode == "REAL" and not regime_ok and not is_regime_override:
            try:
                from adaptive_thresholds import threshold_age_note
                age_note = threshold_age_note("ENTRY_REGIME_MIN_SCORE")
            except Exception:
                age_note = ""
            _wait_with_preview(
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
            _wait_with_preview(drift_reason)
            db.add(decision)
            entry_details.append({"symbol": cand.symbol, "action": "WAIT",
                                   "reasoning": drift_reason, "risk_verdict": None})
            continue

        if per_share_risk <= 0:
            _wait_with_preview("Computed stop is not below entry — invalid risk setup.")
            db.add(decision)
            entry_details.append({"symbol": cand.symbol, "action": "WAIT",
                                   "reasoning": decision.reasoning, "risk_verdict": None})
            continue

        # ── Gate 5: reward:risk floor ─────────────────────────────────────────
        rr = _reward_risk_ratio(entry_price, stop_price, target_price)
        if rr < MIN_REWARD_RISK_RATIO:
            _wait_with_preview(
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
        if is_regime_override:
            # Extra caution on top of the normal conviction scaling — this
            # trade is going in against a still-weak market read, so it gets
            # a smaller slice of equity than the same candidate would on a
            # healthy regime day.
            adj_risk_pct *= config.ENTRY_REGIME_OVERRIDE_RISK_SCALE
        max_trade_risk = account_state.equity * (adj_risk_pct / 100.0)
        proposed_qty   = max(0, int(max_trade_risk // per_share_risk))

        if proposed_qty <= 0:
            _wait_with_preview(
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
            # 2026-09-01 fix: without this, risk_engine's own per-trade cap
            # check re-derives max_trade_risk from the raw (non-conviction)
            # account.risk_per_trade_pct and silently claws proposed_qty
            # back down to the base-percentage amount — nullifying the
            # conviction upsize we just computed above.
            adj_risk_pct=adj_risk_pct,
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
            # 2026-09-03 fix: this branch has always labeled the candidate
            # "WAIT" (both decision.action and the entry_details row below)
            # — correctly, since risk_engine's REJECTED verdicts here are
            # temporary conditions (position cap full, per-trade risk cap,
            # etc. — see risk_engine/engine.py) that can clear on a later
            # cycle, not permanent rejections. But the summary counter
            # incremented was `rejected`, not `waited` — so the dashboard's
            # top-line "N waited / M rejected" never matched the actual
            # per-row "WAIT ..." labels underneath it (e.g. 18 rows all
            # reading "WAIT ... 3 positions already open" while the summary
            # said "2 waited · 18 rejected"). Counting `waited` here makes
            # the summary agree with what's actually shown per-candidate.
            # TradeRiskEvent (above) still separately records the granular
            # risk_engine verdict/reason for every check — unaffected.
            decision.action = "WAIT"
            waited += 1
            db.add(decision)
            entry_details.append({"symbol": cand.symbol, "action": "WAIT",
                                   "reasoning": result.reason, "risk_verdict": result.verdict.value})
            continue

        # ── Approved — create order ────────────────────────────────────────────
        decision.action = "ENTER"
        if is_regime_override:
            decision.reasoning = (
                f"Regime override: score {market_score} < gate {threshold} ({threshold_src}), "
                f"but this was the top-conviction candidate this cycle (conviction "
                f"{cand.conviction_score}) — let through at {config.ENTRY_REGIME_OVERRIDE_RISK_SCALE:.0%} "
                "of normal risk sizing instead of blocking every entry until the regime recovers."
            )
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
            # 2026-09-02 Short-Term Trading Upgrade: carry the watchlist origin
            # forward so portfolio.py can stamp the resulting TradePosition.
            watchlist_entry_id=getattr(cand, "watchlist_entry_id", None),
        )
        db.add(order)
        db.flush()
        db.add(models.TradeOrderEvent(
            order_id=order.id, event_type="PLACED",
            detail=(
                f"Limit ₹{entry_price:.2f} | stop ₹{stop_price:.2f} | "
                f"target ₹{target_price:.2f} | R:R {rr:.2f} | "
                f"conviction {cand.conviction_score} | adj_risk {adj_risk_pct:.2f}% | "
                f"regime_score {market_score} (gate={threshold},{threshold_src})"
                f"{' | REGIME OVERRIDE' if is_regime_override else ''} | "
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
                    f"📤 *BUY sent (auto)*{' 🟡 REGIME OVERRIDE' if is_regime_override else ''} — {cand.symbol}\n"
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
    """Expire PLACED entry orders whose ENTRY_VALIDITY_MINUTES window has
    closed unfilled (config.ENTRY_NO_CHASE — no chase, re-evaluate next
    cycle).

    BUG FIX (2026-09-01): for REAL this used to just flip the LOCAL status
    to EXPIRED and stop there. ENTRY_VALIDITY_MINUTES (15m) is a
    Stockky-side "cancel-and-reassess" window — it has nothing to do with
    the order's actual validity at Dhan (dhan_client.place_order defaults
    to validity="DAY"), so the real limit order was left resting live at
    the exchange for the rest of the trading day. The moment this function
    marked it EXPIRED, reconcile_real_orders() — which only ever looks at
    orders still status=="PLACED" — would stop checking it forever. If
    that resting order filled later in the day, real shares would be
    bought with real money and Stockky would never find out: no position
    opened, no cash debited, no notification, invisible everywhere in the
    app. Same policy as the manual /orders/{mode}/{id}/cancel route and
    dhan_client's own docstring (cancelling is never gated by armed state):
    tell Dhan first, only mark EXPIRED locally once Dhan confirms there's
    nothing left resting. If the cancel call itself fails — which can
    legitimately mean "it already filled" or "it was already
    cancelled/rejected" — don't guess either way; leave the order PLACED
    so reconcile_real_orders() resolves it against Dhan's own order book
    next cycle instead of this function silently declaring it dead.
    """
    now   = datetime.now(timezone.utc)
    stale = (
        db.query(models.TradeOrder)
        .filter(
            models.TradeOrder.mode        == mode,
            # "PARTIAL" (Dhan PART_TRADED, reconcile.py) included alongside
            # "PLACED": a partially-filled entry order still has a resting
            # remainder at Dhan past ENTRY_VALIDITY_MINUTES just like a
            # fully-unfilled one does, and needs the same cancel-the-rest
            # treatment — the shares that DID fill are already booked into
            # a position independently of this order-status transition.
            models.TradeOrder.status.in_(("PLACED", "PARTIAL")),
            models.TradeOrder.valid_until.isnot(None),
            models.TradeOrder.valid_until  < now,
        )
        .all()
    )
    expired = 0
    for order in stale:
        if mode == "REAL" and order.dhan_order_id:
            try:
                dhan_client.cancel_order(db, is_armed=True, dhan_order_id=order.dhan_order_id)
            except Exception as e:
                logger.warning(
                    "expire_stale_orders: Dhan cancel failed for %s (order %s) — "
                    "leaving PLACED so reconcile can resolve it against Dhan: %s",
                    order.symbol, order.dhan_order_id, e,
                )
                await notify_async(
                    f"⚠️ *Entry window closed but Dhan cancel failed* — {order.symbol}\n"
                    f"{str(e)[:300]}\n"
                    "Order left PLACED — reconcile will check next cycle in case it already filled."
                )
                continue

        # A PARTIAL order still gets the SAME terminal "EXPIRED" order
        # status here — the order itself is done (no more shares coming,
        # remainder cancelled at Dhan above), and how many shares DID fill
        # already lives independently in order.filled_qty_so_far and the
        # TradePosition reconcile opened for them, not in this label.
        was_partial = order.status == "PARTIAL"
        order.status     = "EXPIRED"
        order.updated_at = now
        fill_note = f" {order.filled_qty_so_far}/{order.qty} had already filled." if was_partial else ""
        db.add(models.TradeOrderEvent(
            order_id=order.id, event_type="EXPIRED",
            detail=("Entry window closed partially filled — no chase for the rest." if was_partial
                    else "Entry window closed unfilled — no chase.") + fill_note + (
                " Dhan order cancelled." if mode == "REAL" and order.dhan_order_id else ""
            ),
        ))
        expired += 1
    if expired:
        db.commit()
        log_action(db, actor="system", action="ORDERS_EXPIRED", mode=mode,
                   detail=f"count={expired}")
    return expired


# ── Short-Term Trading Upgrade (2026-09-02) ─────────────────────────────────
# Stage 2 trigger pass: read active WatchlistEntry rows, check price hasn't
# run past entry_band_pct from catalyst_price, and — if still within band —
# insert a tagged TradeCandidate with watchlist_entry_id set so it flows into
# the EXISTING evaluate_mode pipeline (which already has ATR/resistance/volume/
# risk checks). This keeps one entry pipeline instead of two.

async def evaluate_watchlist_entries(db: Session, mode: str) -> dict:
    """
    Trigger pass over active WatchlistEntry rows.

    For each entry:
      - Skip if already tracked in TradeCandidate (unconsumed, same mode+symbol).
      - Fetch current price.
      - If catalyst_price == 0.0: set it from the current tick.
          Tier 3 rows: skip to next cycle (price just established, band-check
          next time — by design). Tier 1/2 rows: fall through immediately
          (price was missing at api-gateway ingest, not a deliberate sentinel).
      - If price has moved more than entry_band_pct above catalyst_price → mark
        "missed" and skip (the stock already ran, don't chase).
      - Otherwise insert a TradeCandidate tagged with this watchlist_entry_id
        (decision_label="BUY NOW", source_tab="watchlist") so evaluate_mode
        picks it up in the same or next cycle.

    Returns a tally dict for logs / dashboard.
    """
    active = (
        db.query(models.WatchlistEntry)
        .filter_by(mode=mode, status="active")
        .all()
    )
    if not active:
        return {"watchlist_checked": 0, "band_ok": 0, "missed": 0, "queued": 0}

    symbols = list({row.symbol for row in active})
    ticks = await get_quotes(symbols)

    already_queued_symbols = {
        c.symbol
        for c in db.query(models.TradeCandidate)
        .filter_by(mode=mode, consumed=False)
        .filter(models.TradeCandidate.watchlist_entry_id.isnot(None))
        .all()
    }

    band_ok = missed = queued = 0
    now = datetime.now(timezone.utc)

    for row in active:
        tick = ticks.get(row.symbol)
        if tick is None:
            continue  # no price — skip this cycle, try again next

        price = tick.price

        # catalyst_price == 0.0 means the price was not known at insert time.
        # Two distinct cases:
        #   Tier 3 (volume_shock): sources.py intentionally sets catalyst_price=None
        #     (stored as 0.0) — the catalyst IS the volume event, so the right
        #     baseline is the first price we actually see. Skip this cycle so
        #     the NEXT cycle's band-check is anchored to a real observation, not
        #     the same tick that just triggered discovery. This was the original
        #     design and is correct.
        #   Tier 1 / Tier 2: catalyst_price SHOULD have come from the api-gateway
        #     ("price" / "close" / "cmp" fields in sources.py). If it arrived as
        #     0.0 it means those fields were missing/null in the upstream payload
        #     (e.g. GROWW, MCX, HONASA, GLAXO, BEML, NCC in the 2026-09-04 audit).
        #     In this case we still set the price now, but there is no reason to
        #     skip — the entry is fully known, we just lacked the price at ingest.
        #     Fall through to the band-check immediately so these rows don't sit
        #     NEVER TOUCHED until the next cycle (which may not come until the
        #     next market session for intraday-created entries).
        if row.catalyst_price == 0.0:
            row.catalyst_price = price
            row.updated_at = now
            db.commit()
            if row.source_tier == 3:
                continue  # Tier 3: begin monitoring from next cycle (by design)

        pct_move = (price - row.catalyst_price) / row.catalyst_price

        if pct_move > row.entry_band_pct:
            # Price has run past the entry band — the catalyst edge is gone.
            row.status = "missed"
            row.missed_reason = (
                f"price moved {pct_move:.1%} from catalyst ₹{row.catalyst_price:.2f} "
                f"(band was {row.entry_band_pct:.1%})"
            )
            row.updated_at = now
            db.commit()
            missed += 1
            logger.info(
                "watchlist trigger[%s/%s]: MISSED — %s",
                mode, row.symbol, row.missed_reason,
            )
            continue

        band_ok += 1

        # Already has an unconsumed watchlist-sourced candidate waiting — skip.
        if row.symbol in already_queued_symbols:
            continue

        # Within band and not yet queued — insert a tagged TradeCandidate.
        cand = models.TradeCandidate(
            mode=mode,
            symbol=row.symbol,
            source_tab="watchlist",
            decision_label="BUY NOW",
            conviction_score=row.conviction_score,
            signal_price=price,
            raw_payload=None,
            consumed=False,
            watchlist_entry_id=row.id,
        )
        db.add(cand)
        already_queued_symbols.add(row.symbol)
        queued += 1
        logger.info(
            "watchlist trigger[%s/%s]: QUEUED — catalyst=%s tier=%d "
            "catalyst_px=₹%.2f current_px=₹%.2f move=%.2f%%",
            mode, row.symbol, row.catalyst_type, row.source_tier,
            row.catalyst_price, price, pct_move * 100,
        )

    if queued:
        db.commit()

    return {
        "watchlist_checked": len(active),
        "band_ok": band_ok,
        "missed": missed,
        "queued": queued,
    }
