"""
risk_engine/engine.py — absolute veto authority over every order intent.

This is a REAL, callable implementation of all 9 checks from the frozen
spec, not a placeholder — but as of Phase 1 nothing in this service calls
evaluate() with a live order yet (entry_engine/exit_engine are Phase 2), so
in practice every call path today still ends in no orders being placed.
That's intentional: the risk engine is built and unit-testable in isolation
BEFORE anything is wired to actually spend money through it, per the
phased build order.

Every check is synchronous, in-process, and evaluated in a fixed order —
the first failing check wins and short-circuits the rest (no partial/soft
overrides). AI/entry/exit logic can propose; only this module can approve.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from tz_utils import as_aware
from enum import Enum
from typing import Optional

logger = logging.getLogger("real-trade-risk-engine")


class RiskVerdict(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    BLOCKED_GLOBAL = "blocked_global"  # trading paused account-wide, not just this order


@dataclass
class OrderIntent:
    """What entry_engine/exit_engine propose — never what actually gets
    sent to Dhan. Only a RiskResult with APPROVED lets execution/ act on
    this."""
    mode: str                 # "DEMO" | "REAL"
    symbol: str
    side: str                 # "BUY" | "SELL"
    qty: int
    entry_price: float
    stop_price: float
    target_price: Optional[float] = None
    market_data_timestamp: Optional[datetime] = None
    recent_atr_pct: Optional[float] = None   # for the abnormal-volatility check
    latest_tick_move_pct: Optional[float] = None


@dataclass
class AccountState:
    """Pulled fresh from trade_accounts + trade_risk_config for the
    relevant mode immediately before each evaluate() call — never cached
    across calls, since risk limits must always reflect the current DB
    row, not a snapshot from service startup."""
    equity: float
    risk_per_trade_pct: float
    max_daily_loss_pct: float
    max_concurrent_positions: int
    max_portfolio_risk_pct: float
    stale_data_seconds: int
    max_tick_volatility_mult: float
    allow_pyramiding: bool
    realized_pnl_today: float
    open_position_count: int
    open_position_symbols: set
    open_positions_total_risk: float   # sum of (entry-stop distance * qty) across all open positions
    trading_globally_paused: bool
    market_is_open: bool


@dataclass
class RiskResult:
    verdict: RiskVerdict
    check_name: str
    reason: str
    approved_qty: Optional[int] = None  # risk engine may down-size, never up-size, a proposed qty


def _order_risk_amount(intent: OrderIntent) -> float:
    per_share_risk = abs(intent.entry_price - intent.stop_price)
    return per_share_risk * intent.qty


def evaluate(intent: OrderIntent, account: AccountState, now: Optional[datetime] = None) -> RiskResult:
    """The 9 hard checks, in order. Returns on the FIRST failure — later
    checks never run once one has failed, so the reason reported is always
    the actual blocking cause, not a coincidentally-later one."""
    now = now or datetime.now(timezone.utc)

    # 1. Global pause / disarmed
    if account.trading_globally_paused:
        return RiskResult(RiskVerdict.BLOCKED_GLOBAL, "global_pause",
                           "Trading is paused/disarmed account-wide — no new orders.")

    # 2. Daily loss limit
    daily_loss_pct = (-account.realized_pnl_today / account.equity * 100.0) if account.equity > 0 else 0.0
    if daily_loss_pct >= account.max_daily_loss_pct:
        return RiskResult(RiskVerdict.BLOCKED_GLOBAL, "daily_loss_limit",
                           f"Daily loss {daily_loss_pct:.2f}% has reached the {account.max_daily_loss_pct:.2f}% cap. "
                           f"No new trades for the rest of the trading day.")

    # 3. Max concurrent positions (BUY only — SELL/exit orders must never be
    #    blocked by a positions-count check, or the risk engine could trap
    #    the account in a position it can't close)
    if intent.side == "BUY" and account.open_position_count >= account.max_concurrent_positions:
        return RiskResult(RiskVerdict.REJECTED, "max_concurrent_positions",
                           f"{account.open_position_count} positions already open "
                           f"(cap {account.max_concurrent_positions}).")

    # 4. Per-trade risk cap
    order_risk = _order_risk_amount(intent)
    max_trade_risk = account.equity * (account.risk_per_trade_pct / 100.0)
    if intent.side == "BUY" and order_risk > max_trade_risk:
        # Down-size instead of an outright reject when the entry/stop
        # distance itself is sound — only the quantity is too large.
        per_share_risk = abs(intent.entry_price - intent.stop_price)
        if per_share_risk <= 0:
            return RiskResult(RiskVerdict.REJECTED, "per_trade_risk_cap",
                               "Stop price equals entry price — cannot size a valid risk amount.")
        safe_qty = int(max_trade_risk // per_share_risk)
        if safe_qty <= 0:
            return RiskResult(RiskVerdict.REJECTED, "per_trade_risk_cap",
                               f"Even 1 share risks more than the {account.risk_per_trade_pct:.2f}% per-trade cap.")
        return RiskResult(RiskVerdict.APPROVED, "per_trade_risk_cap",
                           f"Qty down-sized from {intent.qty} to {safe_qty} to respect "
                           f"{account.risk_per_trade_pct:.2f}% per-trade risk cap.",
                           approved_qty=safe_qty)

    # 5. Portfolio-level risk cap
    if intent.side == "BUY":
        prospective_total_risk = account.open_positions_total_risk + order_risk
        max_portfolio_risk = account.equity * (account.max_portfolio_risk_pct / 100.0)
        if prospective_total_risk > max_portfolio_risk:
            return RiskResult(RiskVerdict.REJECTED, "max_portfolio_risk",
                               f"Adding this position would bring total open risk to "
                               f"₹{prospective_total_risk:.2f}, above the "
                               f"{account.max_portfolio_risk_pct:.2f}% portfolio cap.")

    # 6. No pyramiding (unless explicitly allowed)
    if intent.side == "BUY" and not account.allow_pyramiding and intent.symbol in account.open_position_symbols:
        return RiskResult(RiskVerdict.REJECTED, "no_pyramiding",
                           f"{intent.symbol} already has an open position and pyramiding is disabled.")

    # 7. Stale market data
    if intent.market_data_timestamp is not None:
        age_seconds = (now - as_aware(intent.market_data_timestamp)).total_seconds()
        if age_seconds > account.stale_data_seconds:
            return RiskResult(RiskVerdict.REJECTED, "stale_market_data",
                               f"Market data for {intent.symbol} is {age_seconds:.0f}s old "
                               f"(cap {account.stale_data_seconds}s).")

    # 8. Abnormal single-tick volatility
    if intent.recent_atr_pct and intent.latest_tick_move_pct is not None:
        if abs(intent.latest_tick_move_pct) > intent.recent_atr_pct * account.max_tick_volatility_mult:
            return RiskResult(RiskVerdict.REJECTED, "abnormal_volatility",
                               f"Latest tick moved {intent.latest_tick_move_pct:.2f}%, more than "
                               f"{account.max_tick_volatility_mult:.1f}x the recent ATR% "
                               f"({intent.recent_atr_pct:.2f}%) — likely a data glitch or a gap event.")

    # 9. Market hours
    if not account.market_is_open:
        return RiskResult(RiskVerdict.REJECTED, "market_closed",
                           "Market is not open — no order can be placed right now.")

    return RiskResult(RiskVerdict.APPROVED, "all_checks_passed", "All risk checks passed.", approved_qty=intent.qty)
