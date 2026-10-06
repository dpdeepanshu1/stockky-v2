"""orders/cost_gate.py - skip a scalp entry whose target cannot pay for its own costs (group 218, 2026-10-07).

WHY: real-trade-service compares a candidate's expected edge with its round-trip transaction cost (cost_model.py,
Gate 5.6). This service never did, so the review's "no cost check in the scalp service" (item 5) was true: a scalp was
bought whatever its costs were. The recorded P&L here is price-based (no charges field), so costs never showed up
anywhere either.

WHAT: edge = qty * entry * target_pct; cost = INTRADAY statutory levies for a buy at entry and a sell at the target
(brokerage per leg, STT on the sell, exchange + SEBI on both legs, GST on brokerage + exchange + SEBI, stamp duty on the
buy) plus SCALP_COST_SLIPPAGE_ALLOWANCE_PCT of the trade value for spread / slippage. The entry is rejected when
edge / cost < SCALP_MIN_EDGE_TO_COST_RATIO. Rates come from config (same env names as real-trade-service).

HONEST LIMIT: at Rs 0 brokerage the levies are only about 0.04% of trade value, so with the 0.10% allowance the cost is
roughly 0.14% against a 1.4-3.5% target (ratio about 10-25): the gate then passes everything. It only bites when
BROKERAGE_PER_ORDER is set (a flat fee on a small position), when the target is tiny, or when the allowance is raised.
Pure functions, no DB, no network, never raises into the caller. Off with SCALP_COST_GATE_ENABLED=0.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import config

logger = logging.getLogger("position-stocks-cost-gate")


@dataclass
class CostGateResult:
    trade_value: float
    expected_edge: float
    levies: float
    slippage_allowance: float
    cost: float
    ratio: Optional[float]  # None when cost == 0
    passes: bool

    @property
    def cost_pct(self) -> float:
        return (self.cost / self.trade_value * 100.0) if self.trade_value > 0 else 0.0


def estimate_levies(entry_price: float, qty: int, exit_price: Optional[float] = None) -> float:
    """Rs statutory + brokerage cost of an INTRADAY buy at entry_price and sell at exit_price (default: entry)."""
    exit_price = entry_price if exit_price is None else exit_price
    buy_value = entry_price * qty
    sell_value = exit_price * qty
    brokerage = config.BROKERAGE_PER_ORDER * 2
    stt = sell_value * (config.STT_INTRADAY_SELL_PCT / 100.0)
    stamp = buy_value * (config.STAMP_DUTY_BUY_PCT_INTRADAY / 100.0)
    exchange = (buy_value + sell_value) * (config.EXCHANGE_TXN_PCT / 100.0)
    sebi = (buy_value + sell_value) * (config.SEBI_TURNOVER_PCT / 100.0)
    gst = (brokerage + exchange + sebi) * (config.GST_PCT / 100.0)
    return brokerage + stt + stamp + exchange + sebi + gst


def evaluate(entry_price: float, qty: int, target_pct: float) -> CostGateResult:
    trade_value = entry_price * qty
    target_price = entry_price * (1.0 + target_pct / 100.0)
    expected_edge = (target_price - entry_price) * qty
    levies = estimate_levies(entry_price, qty, exit_price=target_price)
    allowance = trade_value * (config.SCALP_COST_SLIPPAGE_ALLOWANCE_PCT / 100.0)
    cost = levies + allowance
    ratio = (expected_edge / cost) if cost > 0 else None
    return CostGateResult(
        trade_value=trade_value, expected_edge=expected_edge, levies=levies,
        slippage_allowance=allowance, cost=cost, ratio=ratio,
        passes=(ratio is None) or (ratio >= config.SCALP_MIN_EDGE_TO_COST_RATIO),
    )


def reject_reason(entry_price: float, qty: int, target_pct: float) -> Optional[str]:
    """None when the entry may go ahead (or the gate is off / cannot be evaluated), else the skip reason."""
    if not config.SCALP_COST_GATE_ENABLED:
        return None
    try:
        if not entry_price or entry_price <= 0 or not qty or qty <= 0 or target_pct is None:
            return None
        r = evaluate(float(entry_price), int(qty), float(target_pct))
    except Exception as e:  # a maths / config problem must never block or crash an entry
        logger.debug("cost gate: evaluate failed, allowing entry: %s", e)
        return None
    if r.passes:
        return None
    return (
        f"edge_to_cost={r.ratio:.2f}<{config.SCALP_MIN_EDGE_TO_COST_RATIO:.2f} "
        f"edge=Rs{r.expected_edge:.2f} cost=Rs{r.cost:.2f}({r.cost_pct:.2f}%) "
        f"qty={qty} value=Rs{r.trade_value:.2f} target={target_pct:.2f}%"
    )
