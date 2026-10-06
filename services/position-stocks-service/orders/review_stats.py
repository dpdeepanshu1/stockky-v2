"""Read-only trade breakdown for tuning decisions (group 167).

Groups closed scalp trades by entry time (30-minute IST bucket), scan window and exit
status, so choices such as the entry window can be made from the trades themselves.
Pure function over ScalpPosition rows; changes nothing."""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Optional

from orders import excursion, trade_stats
from tz_utils import as_aware, ist_now

_NOT_CLOSED = ("OPEN", "EXIT_LEGS_REJECTED", "ERROR")


def _bucket(opened_at) -> str:
    t = ist_now(as_aware(opened_at))
    return f"{t.hour:02d}:{0 if t.minute < 30 else 30:02d}"


def _agg(rows: list) -> dict:
    pnls = [float(r.realized_pnl) for r in rows]
    wins = [p for p in pnls if p > 0]
    gains = [g for g in (excursion.excursion_pcts(r)["max_gain_pct"] for r in rows) if g is not None]
    dds = [d for d in (excursion.excursion_pcts(r)["max_drawdown_pct"] for r in rows) if d is not None]
    return {
        "trades": len(rows),
        "wins": len(wins),
        "win_rate_pct": round(100.0 * len(wins) / len(rows), 1),
        "total_pnl": round(sum(pnls), 2),
        "avg_pnl": round(sum(pnls) / len(rows), 2),
        "avg_max_gain_pct": round(sum(gains) / len(gains), 2) if gains else None,
        "avg_max_drawdown_pct": round(sum(dds) / len(dds), 2) if dds else None,
    }


def breakdown(rows: Iterable, *, min_trades_note: int = 5) -> dict:
    """rows: ScalpPosition-like objects. Only closed rows with a realized P&L count."""
    # group207: only settled trades (a real exit price) — exits still on the entry-price placeholder are
    # not 0-rupee losses; they are left out until reconcile books the real fill.
    closed = [r for r in rows
              if r.status not in _NOT_CLOSED and r.realized_pnl is not None and r.opened_at is not None
              and trade_stats.classify(r) in ("win", "loss", "breakeven")]
    out: dict = {"trades": len(closed), "overall": _agg(closed) if closed else None,
                 "by_entry_time_ist": {}, "by_window": {}, "by_exit_status": {},
                 "note": (f"buckets with fewer than {min_trades_note} trades are noise; "
                          "decide on groups with more evidence")}
    if not closed:
        return out
    groups = {"by_entry_time_ist": defaultdict(list), "by_window": defaultdict(list),
              "by_exit_status": defaultdict(list)}
    for r in closed:
        groups["by_entry_time_ist"][_bucket(r.opened_at)].append(r)
        groups["by_window"][r.window_source or "?"].append(r)
        groups["by_exit_status"][r.status].append(r)
    for name, g in groups.items():
        out[name] = {k: _agg(v) for k, v in sorted(g.items())}
    return out
