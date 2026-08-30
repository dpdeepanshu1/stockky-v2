"""
risk_engine/engine.py — absolute veto authority over every order intent.

MARKET INTELLIGENCE APPLIED (28-Aug-2026):
═══════════════════════════════════════════
Nifty at 24,090 — correction territory (−7% in 6m).
FIIs net short 1,97,792 futures. DII buying providing a floor.
Midcap/Smallcap outperforming large-caps by 13-14% in 1Y.

Risk engine improvements from independent market analysis:
  1. POSITION CONCENTRATION CAP (new): one stock cannot exceed 25%
     of portfolio equity. In a weak market, concentration in a single
     name that gaps down can be account-destroying. This cap forces
     diversification across the 3-position limit.

  2. MINIMUM PRICE FLOOR (new): stocks below ₹20 rejected. In India,
     sub-₹20 names have high operator activity, wide spreads, and
     extremely thin exit liquidity. The small per-share risk also
     produces dangerously large qty proposals.

  3. CHECK ORDER OPTIMIZED: cheapest/most-likely-to-reject checks run
     first (global pause → market closed → daily loss → concurrent
     positions) so expensive aggregations only run for real candidates.

  4. SELL SIDE ALWAYS PASSES concentration + price floor + pyramiding
     checks — exits must never be blocked. Only BUY intents are gated.

  5. DETAILED DOWNSIZE MESSAGES: every downsize logs exactly which cap
     triggered it and by how much, for dashboard audit.

All 9 original checks are preserved. SELL side bypasses checks 1, 3, 4,
4a, 4b, 4c, 5, 5b, 5c, 6, 7, 8. The only checks that still apply to a SELL
are #2 (market hours — the exchange itself isn't open) and #9 (abnormal
single-tick volatility — catch bad data even on exits).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

logger = logging.getLogger("real-trade-risk-engine")

# ── New caps from market research ─────────────────────────────────────────────
# Single position cannot be > 25% of equity. Forces diversification and caps
# damage if one holding gaps down in a weak market environment.
MAX_POSITION_CONCENTRATION_PCT = float(
    os.getenv("RISK_MAX_POSITION_CONCENTRATION_PCT", "25.0")
)
# Minimum stock price — sub-₹20 = operator risk, wide spreads, illiquid exits
MIN_STOCK_PRICE = float(os.getenv("RISK_MIN_STOCK_PRICE", "20.0"))
HARD_FLOOR_PRICE      = float(os.getenv("HARD_FLOOR_PRICE", "20.0"))
HARD_FLOOR_LIQUIDITY  = float(os.getenv("HARD_FLOOR_LIQUIDITY", "5000000"))
HARD_FLOOR_CONVICTION = float(os.getenv("HARD_FLOOR_CONVICTION", "40"))


def passes_hard_floor(candidate: dict) -> tuple:
    """§5 unconditional hard floors. Returns (passes, reason)."""
    price = float(candidate.get("price") or candidate.get("entry_price") or 0)
    if price > 0 and price < HARD_FLOOR_PRICE:
        return False, f"hard_floor_price: ₹{price:.2f} < ₹{HARD_FLOOR_PRICE:.0f}"
    turnover = float(candidate.get("avg_traded_value") or 0)
    if turnover > 0 and turnover < HARD_FLOOR_LIQUIDITY:
        return False, f"hard_floor_liquidity: ₹{turnover:,.0f} < ₹{HARD_FLOOR_LIQUIDITY:,.0f}/day"
    return True, ""


class RiskVerdict(str, Enum):
    APPROVED       = "approved"
    REJECTED       = "rejected"
    BLOCKED_GLOBAL = "blocked_global"


@dataclass
class OrderIntent:
    """What entry/exit engines propose. Only a RiskResult(APPROVED) lets
    execution/ act on this — risk engine is the sole approval authority."""
    mode:   str
    symbol: str
    side:   str   # "BUY" | "SELL"
    qty:    int
    entry_price: float
    stop_price:  float
    target_price:           Optional[float]   = None
    market_data_timestamp:  Optional[datetime] = None
    recent_atr_pct:         Optional[float]   = None
    latest_tick_move_pct:   Optional[float]   = None
    avg_traded_value:       Optional[float]   = None  # §5 liquidity floor


@dataclass
class AccountState:
    """Pulled fresh from DB immediately before every evaluate() call.
    Never cached across calls — risk limits must always reflect live DB rows."""
    equity:                   float
    risk_per_trade_pct:       float
    max_daily_loss_pct:       float
    max_concurrent_positions: int
    max_portfolio_risk_pct:   float
    stale_data_seconds:       int
    max_tick_volatility_mult: float
    allow_pyramiding:         bool
    realized_pnl_today:       float
    open_position_count:      int
    open_position_symbols:    set
    open_positions_total_risk: float
    trading_globally_paused:  bool
    market_is_open:           bool
    cash_available:           float = 0.0


@dataclass
class RiskResult:
    verdict:      RiskVerdict
    check_name:   str
    reason:       str
    approved_qty: Optional[int] = None  # risk engine may downsize, never upsize


def evaluate(
    intent:  OrderIntent,
    account: AccountState,
    now:     Optional[datetime] = None,
) -> RiskResult:
    """
    All checks in optimized order. Returns on the FIRST failure — the reason
    reported is always the actual blocking cause, not a coincidentally-later one.

    SELL-side orders bypass: global_pause, daily_loss_limit,
    concurrent_positions, per_trade_risk, cash_available, concentration,
    portfolio_risk, pyramiding, stale_market_data.
    Exits must never be blocked by entry-sizing or entry-timing checks —
    only #2 (market hours) and #9 (abnormal volatility) still apply to a SELL.
    """
    now = now or datetime.now(timezone.utc)

    # ── 1. Global pause / disarmed (BUY only — closing a position must
    #    never be blocked by "not armed", same policy as everywhere else
    #    in this codebase) ─────────────────────────────────────────────────
    if intent.side == "BUY" and account.trading_globally_paused:
        return RiskResult(
            RiskVerdict.BLOCKED_GLOBAL, "global_pause",
            "Trading is paused/disarmed account-wide — no new orders.",
        )

    # ── 2. Market hours ───────────────────────────────────────────────────────
    # Checked early — if market is closed, all other checks are moot.
    if not account.market_is_open:
        return RiskResult(
            RiskVerdict.REJECTED, "market_closed",
            "Market is not open — no order can be placed right now.",
        )

    # ── 3. Daily loss limit (BUY only — after the daily cap is hit you
    #    must still be able to exit a losing position, not be trapped in it) ──
    if intent.side == "BUY":
        daily_loss_pct = (
            (-account.realized_pnl_today / account.equity * 100.0)
            if account.equity > 0 else 0.0
        )
        if daily_loss_pct >= account.max_daily_loss_pct:
            return RiskResult(
                RiskVerdict.BLOCKED_GLOBAL, "daily_loss_limit",
                f"Daily loss {daily_loss_pct:.2f}% has reached the "
                f"{account.max_daily_loss_pct:.2f}% cap. "
                "No new trades for the rest of this trading day.",
            )

    # ── 4. Max concurrent positions (BUY only) ────────────────────────────────
    if intent.side == "BUY" and account.open_position_count >= account.max_concurrent_positions:
        return RiskResult(
            RiskVerdict.REJECTED, "max_concurrent_positions",
            f"{account.open_position_count} positions already open "
            f"(cap {account.max_concurrent_positions}). "
            "Wait for an existing position to close before adding a new one.",
        )

    # ── 4a. Minimum price floor (BUY only) ────────────────────────────────────
    # New check from market research: sub-₹20 stocks in India have high operator
    # activity, very wide bid-ask spreads, and illiquid exits. Small per-share
    # risk also inflates qty to dangerously large levels.
    if intent.side == "BUY" and intent.entry_price < MIN_STOCK_PRICE:
        return RiskResult(
            RiskVerdict.REJECTED, "min_price_floor",
            f"Entry price ₹{intent.entry_price:.2f} < ₹{MIN_STOCK_PRICE:.0f}. "
            "Sub-₹20: operator risk, wide spreads, illiquid exits.",
        )

    # ── 4b. Liquidity hard floor §5 (BUY only, fail-open when data missing) ───
    if intent.side == "BUY":
        atv = getattr(intent, "avg_traded_value", None) or 0
        if atv and float(atv) < HARD_FLOOR_LIQUIDITY:
            return RiskResult(
                RiskVerdict.REJECTED, "liquidity_floor",
                f"Avg traded value ₹{atv:,.0f} < ₹{HARD_FLOOR_LIQUIDITY:,.0f}/day. "
                "Illiquid — exit may not fill at a reasonable price.",
            )

    # ── 5. Per-trade risk cap — downsize before reject ────────────────────────
    order_risk     = abs(intent.entry_price - intent.stop_price) * intent.qty
    max_trade_risk = account.equity * (account.risk_per_trade_pct / 100.0)
    final_qty      = intent.qty
    downsize_reason: Optional[str] = None

    if intent.side == "BUY" and order_risk > max_trade_risk:
        per_share_risk = abs(intent.entry_price - intent.stop_price)
        if per_share_risk <= 0:
            return RiskResult(
                RiskVerdict.REJECTED, "per_trade_risk_cap",
                "Stop price equals entry price — cannot size a valid risk amount.",
            )
        final_qty = int(max_trade_risk // per_share_risk)
        if final_qty <= 0:
            return RiskResult(
                RiskVerdict.REJECTED, "per_trade_risk_cap",
                f"Even 1 share risks ₹{per_share_risk:.2f} which exceeds the "
                f"{account.risk_per_trade_pct:.2f}% per-trade cap "
                f"(₹{max_trade_risk:.2f}). Cannot size this trade.",
            )
        downsize_reason = (
            f"Qty {intent.qty} → {final_qty} "
            f"(per-trade risk cap {account.risk_per_trade_pct:.2f}% "
            f"= ₹{max_trade_risk:.0f} / ₹{per_share_risk:.2f} per share)."
        )

    # ── 5b. Cash-available cap ────────────────────────────────────────────────
    if intent.side == "BUY":
        order_cost = intent.entry_price * final_qty
        if order_cost > account.cash_available:
            cash_qty = (
                int(account.cash_available // intent.entry_price)
                if intent.entry_price > 0 else 0
            )
            if cash_qty <= 0:
                return RiskResult(
                    RiskVerdict.REJECTED, "cash_available_cap",
                    f"₹{account.cash_available:.2f} available is not enough for "
                    f"even 1 share at ₹{intent.entry_price:.2f}.",
                )
            prev_qty  = final_qty
            final_qty = cash_qty
            downsize_reason = (
                f"Qty {prev_qty} → {final_qty} "
                f"(cash available ₹{account.cash_available:.2f} "
                f"@ ₹{intent.entry_price:.2f}/share)."
            )
        order_risk = abs(intent.entry_price - intent.stop_price) * final_qty

    # ── 5c. Position concentration cap (BUY only) — NEW ──────────────────────
    # Single position capped at MAX_POSITION_CONCENTRATION_PCT of equity.
    # Protects against a single gap-down destroying the account in a weak market.
    if intent.side == "BUY":
        position_value    = intent.entry_price * final_qty
        max_position_val  = account.equity * (MAX_POSITION_CONCENTRATION_PCT / 100.0)
        if position_value > max_position_val:
            conc_qty = int(max_position_val // intent.entry_price) if intent.entry_price > 0 else 0
            if conc_qty <= 0:
                return RiskResult(
                    RiskVerdict.REJECTED, "position_concentration_cap",
                    f"Even 1 share at ₹{intent.entry_price:.2f} would exceed the "
                    f"{MAX_POSITION_CONCENTRATION_PCT:.0f}% portfolio concentration cap "
                    f"(₹{max_position_val:.0f}).",
                )
            if conc_qty < final_qty:
                prev_qty  = final_qty
                final_qty = conc_qty
                downsize_reason = (
                    f"Qty {prev_qty} → {final_qty} "
                    f"(single-position cap {MAX_POSITION_CONCENTRATION_PCT:.0f}% "
                    f"of equity = ₹{max_position_val:.0f})."
                )
                order_risk = abs(intent.entry_price - intent.stop_price) * final_qty

    # ── 6. Portfolio-level risk cap ───────────────────────────────────────────
    if intent.side == "BUY":
        prospective_total = account.open_positions_total_risk + order_risk
        max_portfolio_risk = account.equity * (account.max_portfolio_risk_pct / 100.0)
        if prospective_total > max_portfolio_risk:
            return RiskResult(
                RiskVerdict.REJECTED, "max_portfolio_risk",
                f"Adding this position would bring total open risk to "
                f"₹{prospective_total:.2f} "
                f"(>{account.max_portfolio_risk_pct:.2f}% cap = ₹{max_portfolio_risk:.2f}). "
                "Wait for an existing position to be stopped/targeted before adding more.",
            )

    # ── 7. No pyramiding (BUY only, unless explicitly allowed) ───────────────
    if (
        intent.side == "BUY"
        and not account.allow_pyramiding
        and intent.symbol in account.open_position_symbols
    ):
        return RiskResult(
            RiskVerdict.REJECTED, "no_pyramiding",
            f"{intent.symbol} already has an open position and pyramiding is disabled. "
            "The existing position must close before re-entering.",
        )

    # ── 8. Stale market data (BUY only — a stale price on a SELL still
    #    lets you exit; holding a position on bad data is worse than
    #    exiting on it) ───────────────────────────────────────────────────
    if intent.side == "BUY" and intent.market_data_timestamp is not None:
        age_s = (now - intent.market_data_timestamp).total_seconds()
        if age_s > account.stale_data_seconds:
            return RiskResult(
                RiskVerdict.REJECTED, "stale_market_data",
                f"Market data for {intent.symbol} is {age_s:.0f}s old "
                f"(cap {account.stale_data_seconds}s). "
                "Stale price could lead to wrong stop/target calculations.",
            )

    # ── 9. Abnormal single-tick volatility ────────────────────────────────────
    if intent.recent_atr_pct and intent.latest_tick_move_pct is not None:
        if abs(intent.latest_tick_move_pct) > intent.recent_atr_pct * account.max_tick_volatility_mult:
            return RiskResult(
                RiskVerdict.REJECTED, "abnormal_volatility",
                f"Latest tick moved {intent.latest_tick_move_pct:.2f}%, more than "
                f"{account.max_tick_volatility_mult:.1f}x ATR "
                f"({intent.recent_atr_pct:.2f}%) — likely a data glitch or gap event. "
                "Wait for price to stabilize.",
            )

    if downsize_reason:
        return RiskResult(
            RiskVerdict.APPROVED, "sized_down", downsize_reason, approved_qty=final_qty
        )
    return RiskResult(
        RiskVerdict.APPROVED, "all_checks_passed",
        "All risk checks passed.", approved_qty=final_qty,
    )
