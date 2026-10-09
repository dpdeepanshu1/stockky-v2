"""Read-only trade breakdown for tuning decisions (group 277, plan Phase B).

position-stocks-service has had this since group 167 (orders/review_stats.py); real-trade-service had no way to
see which entry hours and sources earn money, so every rule change was a guess. Pure function over
TradePosition-like rows; changes nothing, calls nothing.

Groups CLOSED rows with a realized P&L by entry time (30-minute IST bucket), source tab and exit hour (IST hour of
closed_at). Each group reports trades, wins, win rate, gross P&L, net P&L (rows that carry net_realized_pnl, i.e.
after the estimated round-trip cost) and expectancy = average P&L per trade (net where known, else gross).
Expectancy is the number to judge a rule by; groups with few trades are noise."""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from tz_utils import as_aware, ist_now

_EPS = 0.005


def _bucket(ts) -> str:
    t = ist_now(as_aware(ts))
    return f"{t.hour:02d}:{0 if t.minute < 30 else 30:02d}"


def _hour(ts) -> str:
    return f"{ist_now(as_aware(ts)).hour:02d}"


def _agg(rows: list) -> dict:
    gross = [float(r.realized_pnl) for r in rows]
    nets = [getattr(r, "net_realized_pnl", None) for r in rows]
    per_trade = [float(n) if n is not None else g for n, g in zip(nets, gross)]
    wins = [p for p in per_trade if p > _EPS]
    net_known = [float(n) for n in nets if n is not None]
    return {
        "trades": len(rows),
        "wins": len(wins),
        "win_rate_pct": round(100.0 * len(wins) / len(rows), 1),
        "gross_pnl": round(sum(gross), 2),
        "net_pnl": round(sum(net_known), 2) if net_known else None,
        "net_known_trades": len(net_known),
        "expectancy": round(sum(per_trade) / len(rows), 2),
    }


def breakdown(rows: Iterable, *, min_trades_note: int = 5) -> dict:
    closed = [r for r in rows
              if getattr(r, "status", None) == "CLOSED" and getattr(r, "realized_pnl", None) is not None
              and getattr(r, "opened_at", None) is not None]
    out: dict = {
        "trades": len(closed),
        "overall": _agg(closed) if closed else None,
        "by_entry_time_ist": {}, "by_source_tab": {}, "by_exit_hour_ist": {},
        "note": (f"groups with fewer than {min_trades_note} trades are noise; expectancy is the average P&L per "
                 "trade (net of the estimated round-trip cost where the row has it, else gross)"),
    }
    if not closed:
        return out
    groups = {"by_entry_time_ist": defaultdict(list), "by_source_tab": defaultdict(list),
              "by_exit_hour_ist": defaultdict(list)}
    for r in closed:
        groups["by_entry_time_ist"][_bucket(r.opened_at)].append(r)
        groups["by_source_tab"][getattr(r, "source_tab", None) or "?"].append(r)
        if getattr(r, "closed_at", None) is not None:
            groups["by_exit_hour_ist"][_hour(r.closed_at)].append(r)
    for name, g in groups.items():
        out[name] = {k: _agg(v) for k, v in sorted(g.items())}
    return out
