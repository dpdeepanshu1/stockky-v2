"""Per-trade measurement report (group 280, plan Phase B: measure before tuning).

Pure function over ScalpPosition-like rows; no DB, no broker, changes nothing. For every SETTLED closed trade it
works out the round-trip charges from the trade's own buy / sell values (same rate card as orders/charges_ledger.py)
and the entry slippage versus the signal price (entry_price is corrected to the real fill by reconcile; signal_price
is what the scanner saw), then groups the trades by exit reason, exit hour (IST), entry hour (IST) and scan window
(the entry tier) and reports for each group: trades, win rate, gross, charges, net, expectancy (net P&L per trade),
average slippage %. Expectancy is the number to judge a rule by; groups with few trades are noise.

A trade is a win/loss on its NET result (after charges), so a trade that makes Rs 3 gross and pays Rs 5 is a loss here
although orders/trade_stats counts it as a win on gross.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Optional

from orders import charges_ledger, trade_stats
from tz_utils import as_aware, ist_now

_EPS = 0.005
_NOT_CLOSED = ("OPEN", "EXIT_LEGS_REJECTED", "ERROR")


def slippage_pct(row) -> Optional[float]:
    """Entry slippage % vs the signal price (positive = paid more than signalled), or None when unknown."""
    sig = getattr(row, "signal_price", None)
    ent = getattr(row, "entry_price", None)
    if not sig or sig <= 0 or not ent or ent <= 0:
        return None
    return (float(ent) - float(sig)) / float(sig) * 100.0


def trade_record(row) -> Optional[dict]:
    """One measured trade, or None when the row is not a settled closed trade."""
    if getattr(row, "status", None) in _NOT_CLOSED:
        return None
    if getattr(row, "realized_pnl", None) is None or getattr(row, "opened_at", None) is None:
        return None
    if trade_stats.classify(row) not in ("win", "loss", "breakeven"):
        return None
    qty = int(getattr(row, "quantity", 0) or 0)
    entry = float(getattr(row, "entry_price", 0) or 0)
    exit_ = float(getattr(row, "exit_price", 0) or 0)
    gross = float(row.realized_pnl)
    charges = charges_ledger.charges_for_trade(entry, exit_, qty)["total"] if qty > 0 and entry > 0 and exit_ > 0 else None
    closed_at = getattr(row, "closed_at", None)
    return {
        "symbol": getattr(row, "symbol", None),
        "tier": getattr(row, "window_source", None) or "?",
        "exit_reason": row.status,
        "entry_hour": f"{ist_now(as_aware(row.opened_at)).hour:02d}",
        "exit_hour": f"{ist_now(as_aware(closed_at)).hour:02d}" if closed_at is not None else None,
        "gross_pnl": gross,
        "charges": charges,
        "net_pnl": gross - charges if charges is not None else None,
        "slippage_pct": slippage_pct(row),
    }


def _agg(recs: list) -> dict:
    per_trade = [r["net_pnl"] if r["net_pnl"] is not None else r["gross_pnl"] for r in recs]
    wins = [p for p in per_trade if p > _EPS]
    slips = [r["slippage_pct"] for r in recs if r["slippage_pct"] is not None]
    charged = [r["charges"] for r in recs if r["charges"] is not None]
    n = len(recs)
    return {
        "trades": n,
        "wins": len(wins),
        "win_rate_pct": round(100.0 * len(wins) / n, 1),
        "gross_pnl": round(sum(r["gross_pnl"] for r in recs), 2),
        "charges": round(sum(charged), 2) if charged else None,
        "net_pnl": round(sum(per_trade), 2),
        "expectancy": round(sum(per_trade) / n, 2),
        "avg_slippage_pct": round(sum(slips) / len(slips), 3) if slips else None,
        "slippage_known_trades": len(slips),
    }


def report(rows: Iterable, *, min_trades_note: int = 5) -> dict:
    recs = [r for r in (trade_record(x) for x in rows) if r is not None]
    out: dict = {"trades": len(recs), "overall": _agg(recs) if recs else None,
                 "by_exit_reason": {}, "by_exit_hour_ist": {}, "by_entry_hour_ist": {}, "by_tier": {},
                 "note": (f"win and loss are judged on NET P&L after estimated charges; groups with fewer than "
                          f"{min_trades_note} trades are noise; avg_slippage_pct is only over trades that recorded a "
                          "signal price")}
    if not recs:
        return out
    groups = {"by_exit_reason": defaultdict(list), "by_exit_hour_ist": defaultdict(list),
              "by_entry_hour_ist": defaultdict(list), "by_tier": defaultdict(list)}
    for r in recs:
        groups["by_exit_reason"][r["exit_reason"]].append(r)
        groups["by_exit_hour_ist"][r["exit_hour"] or "?"].append(r)
        groups["by_entry_hour_ist"][r["entry_hour"]].append(r)
        groups["by_tier"][r["tier"]].append(r)
    for name, g in groups.items():
        out[name] = {k: _agg(v) for k, v in sorted(g.items())}
    return out
