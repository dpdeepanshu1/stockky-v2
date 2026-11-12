"""fno/costs.py - real-world cost of one option round trip (BUY then SELL of premium). Pure.

Per leg:  brokerage = min(flat per order, pct cap x turnover);  exchange transaction charge = pct x turnover;  SEBI fee = pct x turnover;
          GST = pct x (brokerage + exchange + SEBI);  STT only on the SELL premium;  stamp duty only on the BUY premium.
Every rate lives in Params (env-overridable). Round 238 check against published rates (8 Nov 2026): STT 0.15 % of the SELL premium from 1 Apr 2026; NSE transaction charge 0.03553 % of premium from
1 Mar 2026; BSE (Sensex) 0.0325 %; Dhan brokerage on OPTIONS is a flat Rs 20 per EXECUTED order (the "Rs 20 or 0.03 %, whichever is lower" rule is for equity intraday / futures, not options; this is why
brokerage_pct_cap defaults to 100, i.e. no percentage cap); SEBI fee Rs 10 per crore; GST 18 % on brokerage + exchange + SEBI; stamp 0.003 % on the BUY premium. A hand calculation of a 65-lot round trip
matches `round_trip` to the paisa (tests). Round 458 (E479): the first real contract note (Dhan, 11 Nov 2026, one NIFTY CE round trip 65 x 61.10 / 60.05) matched every rate: brokerage 40.00, NSE 2.80, SEBI 0.01, GST 7.71.
It also showed that Dhan rounds STT and stamp duty to WHOLE rupees on the contract note (STT 5.855 -> 6.00, stamp 0.119 -> 0.00; the equity side the same day: STT 5.05 -> 5.00, stamp 0.60 -> 1.00), per segment per day. `round_trip` keeps the exact
figures (an unbiased estimate when many trades share a day); `on_contract_note` gives the whole-rupee view the probe check compares with. An option held into an in-the-money expiry would pay exercise STT instead: the engine never does that (out by 14:45 / 15:15).
Costs do not depend on direction (long-only): a winning trade pays the same fixed charges as a losing one, which is why small premiums are punished."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Dict

from .params import Params


@dataclass
class Cost:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    gst: float
    stamp: float
    total: float

    def to_json(self) -> Dict[str, float]:
        return {k: round(v, 2) for k, v in asdict(self).items()}


def _leg(turnover: float, p: Params, exch_pct: float, sell: bool) -> Cost:
    brokerage = min(p.brokerage_per_order, turnover * p.brokerage_pct_cap / 100.0) if turnover > 0 else 0.0
    exchange = turnover * exch_pct / 100.0
    sebi = turnover * p.sebi_pct / 100.0
    gst = (brokerage + exchange + sebi) * p.gst_pct / 100.0
    stt = turnover * p.stt_sell_pct / 100.0 if sell else 0.0
    stamp = 0.0 if sell else turnover * p.stamp_buy_pct / 100.0
    return Cost(brokerage, stt, exchange, sebi, gst, stamp, brokerage + stt + exchange + sebi + gst + stamp)


def rupee(x: float) -> float:
    """Nearest whole rupee, halves up (Python's round() goes to even, which is not what a contract note does)."""
    return float(math.floor(max(0.0, x) + 0.5))


def on_contract_note(c: Cost) -> Cost:
    """The same charges as Dhan prints them: STT and stamp duty in whole rupees, the other four lines to the paisa. Total is the sum of the printed lines."""
    stt, stamp = rupee(c.stt), rupee(c.stamp)
    b, e, s_, g = (round(v, 2) for v in (c.brokerage, c.exchange, c.sebi, c.gst))
    return Cost(b, stt, e, s_, g, stamp, round(b + stt + e + s_ + g + stamp, 2))


def exch_pct(underlying: str, p: Params) -> float:
    return p.exch_txn_pct_bse if (underlying or "").upper() == "SENSEX" else p.exch_txn_pct_nse


def round_trip(underlying: str, entry_premium: float, exit_premium: float, qty: int, p: Params, child: bool = False) -> Cost:
    """Total charges for buying `qty` units at entry_premium and selling at exit_premium.
    `child=True`: this is a PARTIAL-exit row of a trade whose entry was ONE order. That order's flat brokerage (and the GST on it) is charged to the part that stays open, so it is left out here.
    paper.split_closed and GET /fno/charges both use this, so the stored charges and the page can never disagree."""
    ep = exch_pct(underlying, p)
    buy = _leg(max(0.0, entry_premium) * qty, p, ep, sell=False)
    sell = _leg(max(0.0, exit_premium) * qty, p, ep, sell=True)
    vals = {f: getattr(buy, f) + getattr(sell, f) for f in ("brokerage", "stt", "exchange", "sebi", "gst", "stamp", "total")}
    if child:
        b = min(buy.brokerage, vals["brokerage"])
        g = b * p.gst_pct / 100.0
        vals["brokerage"] -= b
        vals["gst"] = max(0.0, vals["gst"] - g)
        vals["total"] = max(0.0, vals["total"] - b - g)
    return Cost(*(round(vals[f], 4) for f in ("brokerage", "stt", "exchange", "sebi", "gst", "stamp", "total")))


def breakeven_move_pct(underlying: str, entry_premium: float, qty: int, p: Params) -> float:
    """How far the premium must rise (in % of the entry premium) just to pay the charges, assuming the exit price equals the entry price."""
    if entry_premium <= 0 or qty <= 0:
        return 0.0
    c = round_trip(underlying, entry_premium, entry_premium, qty, p)
    return c.total / (entry_premium * qty) * 100.0
