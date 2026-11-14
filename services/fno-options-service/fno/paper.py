"""fno/paper.py - one option position from fill to exit. Pure: no clock, no network, no database. The live engine and ALL the backtests use these same functions,
so a rule changed here changes the paper trades and the backtest in step.

Entry fill (DEMO):  buy at the ask (or the last price when there is no ask) plus slippage; sell at the bid (or last price) minus slippage.
Exit rules, checked in this order on every observation of the premium:
    SQUAREOFF  the end-of-day time (or the contract is about to expire)    exit at the market
    SL         premium <= stop                                             stop = entry x (1 - sl%)
    TRAIL      armed once the premium was >= entry x (1 + arm%); then exit when premium <= best x (1 - trail%) (the stop only ever moves up)
    TARGET     premium >= entry x (1 + target%)
For OHLC bars (backtests) `evaluate_bar` is pessimistic: when a bar's range touches both the stop and the target, the STOP is assumed to hit first.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, Optional, Tuple

from . import costs
from .models import ContractRef
from .params import Params

OPEN, CLOSED = "OPEN", "CLOSED"
SL, TRAIL, TARGET, SQUAREOFF, MANUAL, SIGNAL_FLIP = "SL", "TRAIL", "TARGET", "SQUAREOFF", "MANUAL", "SIGNAL_FLIP"


@dataclass
class Position:
    underlying: str
    expiry: str
    strike: float
    opt_type: str
    lot_size: int
    lots: int
    entry_ts: float
    entry_premium: float                  # the FILL price (slippage included)
    sl_price: float
    target_price: float
    mode: str = "DEMO"
    security_id: Optional[int] = None
    segment: str = ""
    id: Optional[int] = None
    status: str = OPEN
    max_premium: float = 0.0
    min_premium: float = 0.0
    trail_armed: bool = False
    trail_stop: Optional[float] = None
    exit_ts: Optional[float] = None
    exit_premium: Optional[float] = None
    exit_reason: str = ""
    gross_pnl: Optional[float] = None
    charges: Optional[float] = None
    net_pnl: Optional[float] = None
    signal_score: Optional[float] = None
    spot_at_entry: Optional[float] = None
    note: str = ""
    broker_order_ids: Dict[str, str] = field(default_factory=dict)
    strategy: str = "CORE"                # which strategy (fno/strategies.py) opened it; rows written before round 232 load as CORE
    score_parts: Dict[str, float] = field(default_factory=dict)   # round 507: the daily / intraday / chain parts of the entry score, so "why CE" can be read per trade
    parent_id: Optional[int] = None       # a PARTIAL exit books the sold lots as a closed child row of the position that stays open

    @property
    def qty(self) -> int:
        return self.lots * self.lot_size

    @property
    def key(self) -> str:
        return f"{self.underlying}:{self.opt_type}"

    def label(self) -> str:
        s = int(self.strike) if float(self.strike).is_integer() else self.strike
        return f"{self.underlying} {self.expiry} {s} {self.opt_type}"

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Position":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in names})


# ── fills ─────────────────────────────────────────────────────────────────────────────────────────────────
def round_tick(price: float, tick: float = 0.05, up: bool = False) -> float:
    if tick <= 0:
        return round(price, 2)
    n = price / tick
    import math
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * tick, 2)


def buy_fill(ask: Optional[float], ltp: Optional[float], p: Params, tick: float = 0.05) -> Optional[float]:
    base = ask if ask and ask > 0 else ltp
    if not base or base <= 0:
        return None
    return round_tick(base * (1 + p.slippage_pct / 100.0), tick, up=True)


def sell_fill(bid: Optional[float], ltp: Optional[float], p: Params, tick: float = 0.05) -> Optional[float]:
    base = bid if bid and bid > 0 else ltp
    if not base or base <= 0:
        return None
    return max(tick, round_tick(base * (1 - p.slippage_pct / 100.0), tick, up=False))


# ── lifecycle ─────────────────────────────────────────────────────────────────────────────────────────────
def open_position(c: ContractRef, lots: int, fill: float, ts: float, p: Params, mode: str = "DEMO", score: Optional[float] = None,
                  spot: Optional[float] = None, strategy: str = "CORE") -> Position:
    if lots < 1 or fill <= 0:
        raise ValueError("a position needs lots >= 1 and a positive fill")
    return Position(underlying=c.underlying, expiry=c.expiry, strike=c.strike, opt_type=c.opt_type, lot_size=c.lot_size, lots=lots, entry_ts=ts,
                    entry_premium=fill, sl_price=round_tick(fill * (1 - p.sl_pct / 100.0), c.tick_size, up=True),
                    target_price=round_tick(fill * (1 + p.target_pct / 100.0), c.tick_size, up=True), mode=mode, security_id=c.security_id,
                    segment=c.segment, max_premium=fill, min_premium=fill, signal_score=score, spot_at_entry=spot, strategy=strategy)


def _arm_and_trail(pos: Position, premium: float, p: Params) -> None:
    pos.max_premium = max(pos.max_premium or pos.entry_premium, premium)
    pos.min_premium = min(pos.min_premium or pos.entry_premium, premium)
    if not pos.trail_armed and pos.max_premium >= pos.entry_premium * (1 + p.trail_arm_pct / 100.0):
        pos.trail_armed = True
    if pos.trail_armed:
        ts = pos.max_premium * (1 - p.trail_pct / 100.0)
        pos.trail_stop = ts if pos.trail_stop is None else max(pos.trail_stop, ts)


def evaluate_tick(pos: Position, premium: float, p: Params, squareoff: bool = False) -> Optional[Tuple[str, float]]:
    """One observation of the premium. Updates the high / low / trail on the position; returns (reason, reference price) when an exit rule fires, else None."""
    if pos.status != OPEN or premium is None or premium <= 0:
        return None
    _arm_and_trail(pos, premium, p)
    if squareoff:
        return SQUAREOFF, premium
    if premium <= pos.sl_price:
        return SL, premium
    if pos.trail_armed and pos.trail_stop is not None and premium <= pos.trail_stop:
        return TRAIL, premium
    if premium >= pos.target_price:
        return TARGET, premium
    return None


def evaluate_bar(pos: Position, o: float, h: float, l: float, c: float, p: Params, squareoff: bool = False) -> Optional[Tuple[str, float]]:
    """One OHLC bar of the premium (backtests). Pessimistic ordering; a gap through the stop fills at the open, not at the stop price."""
    if pos.status != OPEN:
        return None
    if squareoff:
        _arm_and_trail(pos, c, p)
        return SQUAREOFF, c
    stop = pos.sl_price
    if pos.trail_armed and pos.trail_stop is not None:
        stop = max(stop, pos.trail_stop)
    if o <= stop:
        return (TRAIL if (pos.trail_armed and stop > pos.sl_price) else SL), o
    if l <= stop:
        return (TRAIL if (pos.trail_armed and stop > pos.sl_price) else SL), stop
    if o >= pos.target_price:
        return TARGET, o
    if h >= pos.target_price:
        _arm_and_trail(pos, h, p)
        return TARGET, pos.target_price
    _arm_and_trail(pos, h, p)                                   # the high may arm the trail for the NEXT bar
    if pos.trail_armed and pos.trail_stop is not None and c <= pos.trail_stop:
        return TRAIL, c
    return None


def close_position(pos: Position, exit_fill: float, ts: float, reason: str, p: Params) -> Position:
    """Mark the position closed and compute gross / charges / net. `exit_fill` is the price actually received (slippage already applied by the caller)."""
    pos.status = CLOSED
    pos.exit_ts = ts
    pos.exit_premium = exit_fill
    pos.exit_reason = reason
    pos.gross_pnl = round((exit_fill - pos.entry_premium) * pos.qty, 2)
    pos.charges = round(costs.round_trip(pos.underlying, pos.entry_premium, exit_fill, pos.qty, p).total, 2)
    pos.net_pnl = round(pos.gross_pnl - pos.charges, 2)
    return pos


def split_closed(pos: Position, lots: int, exit_fill: float, ts: float, reason: str, p: Params, order_id: str = "") -> Position:
    """A PARTIAL exit: `lots` of `pos` were sold at `exit_fill`. Returns a CLOSED child row (same contract and entry) that carries that part of the P&L; the caller then
    reduces `pos.lots`. The entry was ONE order, so its flat brokerage (and the GST on it) is charged to the part that stays open, not again here."""
    if lots < 1 or lots >= pos.lots:
        raise ValueError("a partial exit must be at least one lot and less than the position")
    child = Position.from_json({**pos.to_json(), "id": None, "lots": lots, "status": OPEN, "parent_id": pos.id, "broker_order_ids": ({"exit": order_id} if order_id else {})})
    child.note = (pos.note + f" | partial exit of #{pos.id}").strip(" |")
    close_position(child, exit_fill, ts, reason, p)
    child.charges = round(costs.round_trip(child.underlying, child.entry_premium, exit_fill, child.qty, p, child=True).total, 2)
    child.net_pnl = round(child.gross_pnl - child.charges, 2)
    return child


def unrealized(pos: Position, premium: Optional[float]) -> Optional[float]:
    return round((premium - pos.entry_premium) * pos.qty, 2) if (premium is not None and pos.status == OPEN) else None
