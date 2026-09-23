"""
cost_model.py — Real-world transaction-cost estimation for NSE equity round trips.

2026-09-18 (user audit finding): entry_engine and risk_engine had ZERO concept
of brokerage/STT/exchange charges/GST/stamp duty anywhere — a candidate's
theoretical edge (target_pct * position_value) was compared to nothing before
an order got placed. Combined with 1%-of-equity risk sizing on ATR-based
stops, this routinely produced 1-2 share positions on higher-priced stocks,
where these largely-fixed/percentage costs are a much larger fraction of
trade value than they'd be on a bigger position — a very plausible driver of
"breakeven or small loss" outcomes even when the underlying signal has a real
statistical edge.

This module estimates round-trip cost only — it does not place orders, touch
the DB, or import anything from this service besides config. Every rate is
env-overridable via config.py; correct them against a real Dhan contract note
whenever rates change, no code change needed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import config


@dataclass
class CostEstimate:
    brokerage: float
    stt: float
    exchange_txn: float
    sebi_turnover: float
    gst: float
    stamp_duty: float
    dp_charge: float
    total: float


def estimate_round_trip_cost(
    entry_price: float,
    qty: int,
    exit_price: Optional[float] = None,
    product_type: str = "CNC",
    is_delivery_sell: bool = False,
) -> CostEstimate:
    """Estimate the total ₹ cost of buying `qty` shares at `entry_price` and
    selling them at `exit_price` (defaults to entry_price if unknown — a
    reasonable estimate before a target is actually hit).

    product_type: "CNC" (delivery — STT + stamp duty on BOTH/BUY at the
    delivery rate) or "INTRADAY"/"MIS" (STT on SELL only, cheaper stamp
    duty). is_delivery_sell: True only when this is an actual sale of
    previously-settled demat holdings (triggers the flat DP charge) — a
    same-day round trip never sells real holdings, so this should stay
    False for same-day entries regardless of product_type.
    """
    exit_price = exit_price if exit_price is not None else entry_price
    buy_value = entry_price * qty
    sell_value = exit_price * qty
    is_delivery = (product_type or "CNC").upper() == "CNC"

    brokerage = config.BROKERAGE_PER_ORDER * 2  # one per leg

    if is_delivery:
        stt = (buy_value + sell_value) * (config.STT_DELIVERY_PCT_PER_LEG / 100.0)
        stamp_duty = buy_value * (config.STAMP_DUTY_BUY_PCT_DELIVERY / 100.0)
    else:
        stt = sell_value * (config.STT_INTRADAY_SELL_PCT / 100.0)
        stamp_duty = buy_value * (config.STAMP_DUTY_BUY_PCT_INTRADAY / 100.0)

    exchange_txn = (buy_value + sell_value) * (config.EXCHANGE_TXN_PCT / 100.0)
    sebi_turnover = (buy_value + sell_value) * (config.SEBI_TURNOVER_PCT / 100.0)
    gst = (brokerage + exchange_txn + sebi_turnover) * (config.GST_PCT / 100.0)
    dp_charge = config.DP_CHARGE_FLAT * (1 + config.GST_PCT / 100.0) if is_delivery_sell else 0.0

    total = brokerage + stt + exchange_txn + sebi_turnover + gst + stamp_duty + dp_charge
    return CostEstimate(
        brokerage=round(brokerage, 2), stt=round(stt, 2),
        exchange_txn=round(exchange_txn, 2), sebi_turnover=round(sebi_turnover, 2),
        gst=round(gst, 2), stamp_duty=round(stamp_duty, 2),
        dp_charge=round(dp_charge, 2), total=round(total, 2),
    )


@dataclass
class EdgeVsCostResult:
    trade_value: float
    expected_edge: float
    estimated_cost: float
    ratio: Optional[float]  # None when estimated_cost == 0
    passes_min_value: bool
    passes_min_ratio: bool

    @property
    def passes(self) -> bool:
        return self.passes_min_value and self.passes_min_ratio


def evaluate_entry_cost_gate(
    entry_price: float,
    qty: int,
    target_pct: float,
    product_type: str = "CNC",
    min_trade_value: Optional[float] = None,
    min_edge_to_cost_ratio: Optional[float] = None,
) -> EdgeVsCostResult:
    """Gate 5.6 helper (entry_engine/entry.py): compares a candidate's own
    expected ₹ edge (position value * target_pct) against its estimated
    round-trip transaction cost. Same-day entries never trigger DP charges
    (is_delivery_sell=False) since a same-day exit is sold INTRADAY, never a
    real delivery — see exit_engine._send_real_sell's product-type logic.

    min_trade_value/min_edge_to_cost_ratio: 2026-09-18 fix (follow-on item
    #5) — optional per-mode overrides (entry_engine._resolve_cost_gate_knobs
    reads them from TradeRiskConfig). Defaulting to None here (which falls
    back to config.py's env-var values) keeps this function's own behavior
    and every existing caller/test unchanged."""
    trade_value = entry_price * qty
    target_price = entry_price * (1 + target_pct / 100.0)
    expected_edge = (target_price - entry_price) * qty
    cost = estimate_round_trip_cost(
        entry_price, qty, exit_price=target_price,
        product_type=product_type, is_delivery_sell=False,
    )
    ratio = (expected_edge / cost.total) if cost.total > 0 else None
    effective_min_trade_value = min_trade_value if min_trade_value is not None else config.MIN_TRADE_VALUE
    effective_min_ratio = min_edge_to_cost_ratio if min_edge_to_cost_ratio is not None else config.MIN_EDGE_TO_COST_RATIO
    return EdgeVsCostResult(
        trade_value=round(trade_value, 2),
        expected_edge=round(expected_edge, 2),
        estimated_cost=cost.total,
        ratio=round(ratio, 2) if ratio is not None else None,
        passes_min_value=trade_value >= effective_min_trade_value,
        passes_min_ratio=(ratio is None) or (ratio >= effective_min_ratio),
    )
