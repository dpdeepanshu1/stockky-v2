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
    # group 266 (verified on the Dhan contract note of 07-Oct-2026): a CNC buy that is sold the SAME day is charged by Dhan
    # as an intraday round trip. Delivery rates therefore apply only when the exit is a sale of settled holdings
    # (is_delivery_sell=True, i.e. a carried position) - same-day CNC round trips are priced like MIS.
    is_delivery = (product_type or "CNC").upper() == "CNC" and bool(is_delivery_sell)

    if is_delivery:
        brokerage = config.BROKERAGE_PER_ORDER * 2  # one per leg
    else:
        # group 264: an INTRADAY (MIS) leg costs min(Rs cap, pct of value) at Dhan, not the delivery Rs 0 - the gate
        # must see it now that entries are routed to MIS. Never below the configured flat per-order figure.
        brokerage = sum(
            max(config.BROKERAGE_PER_ORDER, min(config.CHARGES_BROKERAGE_CAP_RS, v * config.CHARGES_BROKERAGE_PCT / 100.0))
            for v in (buy_value, sell_value)
        )

    if is_delivery:
        stt = (buy_value + sell_value) * (config.STT_DELIVERY_PCT_PER_LEG / 100.0)
        stamp_duty = buy_value * (config.STAMP_DUTY_BUY_PCT_DELIVERY / 100.0)
    else:
        stt = sell_value * (config.STT_INTRADAY_SELL_PCT / 100.0)
        stamp_duty = buy_value * (config.STAMP_DUTY_BUY_PCT_INTRADAY / 100.0)

    exchange_txn = (buy_value + sell_value) * ((config.EXCHANGE_TXN_PCT + config.IPFT_PCT) / 100.0)   # NSE + IPFT
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
    win_prob: float = 1.0          # round 153: the win chance the edge was weighted by (1.0 = full target)
    weighted_edge: float = 0.0     # round 153: expected_edge x win_prob, the number compared with the cost
    passes_min_net: bool = True    # round 153: weighted_edge - cost >= config.MIN_NET_EDGE_RS (0 = off)

    @property
    def passes(self) -> bool:
        return self.passes_min_value and self.passes_min_ratio and self.passes_min_net


def evaluate_entry_cost_gate(
    entry_price: float,
    qty: int,
    target_pct: float,
    product_type: str = "CNC",
    min_trade_value: Optional[float] = None,
    min_edge_to_cost_ratio: Optional[float] = None,
    include_dp: bool = False,
    win_prob: Optional[float] = None,
) -> EdgeVsCostResult:
    """Gate 5.6 helper (entry_engine/entry.py): compares a candidate's own
    expected ₹ edge (position value * target_pct) against its estimated
    round-trip transaction cost. Same-day entries never trigger DP charges
    (is_delivery_sell=False) since a same-day exit is sold INTRADAY, never a
    real delivery — see exit_engine._send_real_sell's product-type logic.

    min_trade_value/min_edge_to_cost_ratio: 2026-09-18 fix (follow-on item
    #5) — optional per-mode overrides (entry_engine._resolve_cost_gate_knobs
    reads them from TradeRiskConfig). group 264: include_dp=True adds the flat DP charge (Rs 12.50 + GST) to the cost - used for CNC
    entries that may be carried overnight and then sold from demat. Defaulting to None here (which falls
    back to config.py's env-var values) keeps this function's own behavior
    and every existing caller/test unchanged."""
    trade_value = entry_price * qty
    target_price = entry_price * (1 + target_pct / 100.0)
    expected_edge = (target_price - entry_price) * qty
    cost = estimate_round_trip_cost(
        entry_price, qty, exit_price=target_price,
        product_type=product_type, is_delivery_sell=bool(include_dp) and (product_type or "CNC").upper() == "CNC",
    )
    wp = win_prob if win_prob is not None else getattr(config, "COST_GATE_WIN_PROB", 1.0)      # round 153: realistic win chance, not "the full target is hit"
    try:
        wp = float(wp)
        wp = wp if 0.0 < wp <= 1.0 else 1.0
    except (TypeError, ValueError):
        wp = 1.0
    weighted_edge = expected_edge * wp
    ratio = (weighted_edge / cost.total) if cost.total > 0 else None
    effective_min_trade_value = min_trade_value if min_trade_value is not None else config.MIN_TRADE_VALUE
    effective_min_ratio = min_edge_to_cost_ratio if min_edge_to_cost_ratio is not None else config.MIN_EDGE_TO_COST_RATIO
    return EdgeVsCostResult(
        trade_value=round(trade_value, 2),
        expected_edge=round(expected_edge, 2),
        estimated_cost=cost.total,
        ratio=round(ratio, 2) if ratio is not None else None,
        passes_min_value=trade_value >= effective_min_trade_value,
        passes_min_ratio=(ratio is None) or (ratio >= effective_min_ratio),
        win_prob=wp,
        weighted_edge=round(weighted_edge, 2),
        passes_min_net=(weighted_edge - cost.total) >= float(getattr(config, "MIN_NET_EDGE_RS", 0.0) or 0.0),
    )


def min_qty_mode() -> str:
    """E273 round 23: ENTRY_MIN_QTY_MODE read at call time, `cost` (default) or `fixed`; anything else reads as cost."""
    m = str(getattr(config, "ENTRY_MIN_QTY_MODE", "cost") or "cost").strip().lower()
    return m if m in ("cost", "fixed") else "cost"


def effective_min_qty(
    entry_price: float,
    target_pct: float,
    fixed_floor: int,
    min_trade_value: Optional[float] = None,
    min_edge_to_cost_ratio: Optional[float] = None,
) -> int:
    """E273 round 23: the minimum shares for a REAL entry. `fixed` (or an unusable input, or the cost model switched off) = the flat ENTRY_MIN_QTY floor, as before.
    `cost` = the smallest quantity from 1 up to that floor at which the SAME cost gate the entry runs next (Gate 5.6: minimum trade value and edge-to-cost
    ratio) passes; when none does, the floor itself (the cost gate then judges it as before). It is judged as a CNC entry that may be carried (the dearer
    product with the DP charge), so a quantity that passes here also passes the real gate whichever product is chosen. Never raises; never above the flat
    floor, so it can only let a smaller trade through, and only one whose own edge pays for its costs."""
    try:
        floor = int(fixed_floor or 0)
        if floor <= 1 or min_qty_mode() != "cost" or not config.COST_MODEL_ENABLED:
            return floor
        px, tp = float(entry_price), float(target_pct)
        if px <= 0 or tp <= 0:
            return floor
        for q in range(1, floor):
            if evaluate_entry_cost_gate(px, q, tp, product_type="CNC", min_trade_value=min_trade_value,
                                        min_edge_to_cost_ratio=min_edge_to_cost_ratio, include_dp=True).passes:
                return q
        return floor
    except Exception:  # noqa: BLE001 - a maths / config problem must never block or crash an entry
        return int(fixed_floor or 0)


def estimate_delivery_sell_cost(price: float, qty: int, carried: bool = True) -> float:
    """group 264: Rs cost of ONE delivery (CNC) SELL leg of `qty` shares at `price`: STT + exchange + SEBI + GST on those,
    plus the flat DP charge (Rs 12.50 + GST) when `carried` (shares settled in demat from an earlier day - a same-day
    sale never pays DP)."""
    value = float(price) * int(qty)
    stt = value * (config.STT_DELIVERY_PCT_PER_LEG / 100.0)
    exchange = value * ((config.EXCHANGE_TXN_PCT + config.IPFT_PCT) / 100.0)
    sebi = value * (config.SEBI_TURNOVER_PCT / 100.0)
    gst = (exchange + sebi) * (config.GST_PCT / 100.0)
    dp = config.DP_CHARGE_FLAT * (1 + config.GST_PCT / 100.0) if carried else 0.0
    return round(stt + exchange + sebi + gst + dp, 2)


def breakeven_level(entry: float, qty, ltp: float, carried: bool = False) -> float:
    """E285 round 156 (plan item 11): the stop level for the breakeven rule: entry + estimated round-trip charges per share + BREAKEVEN_BUFFER_PCT of entry, so
    being stopped there is about zero NET. Falls back to the plain entry price when BREAKEVEN_COVER_CHARGES is off, the quantity / price is unusable, or the
    covered level would not sit below the current price (a stop at or above the market would fire at once). Never raises; never returns less than entry."""
    try:
        if not getattr(config, "BREAKEVEN_COVER_CHARGES", True) or not entry or entry <= 0 or not qty or int(qty) <= 0:
            return entry
        q = int(qty)
        cost = estimate_round_trip_cost(entry, q, exit_price=entry, product_type="CNC" if carried else "MIS", is_delivery_sell=bool(carried))
        level = round(entry + cost.total / q + entry * float(getattr(config, "BREAKEVEN_BUFFER_PCT", 0.0)) / 100.0, 2)
        return level if entry <= level < float(ltp) else entry
    except Exception:  # noqa: BLE001
        return entry
