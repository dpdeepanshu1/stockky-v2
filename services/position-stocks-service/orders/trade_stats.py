"""Trade-history summary maths (group 207, item 12).

Pure functions over ScalpPosition-like rows; no DB, no broker. The dashboard summary used to count
every non-OPEN row with a P&L as a trade and every P&L <= 0 as a loss, so rows that were not trades
(ERROR rows, exits still waiting for their real fill price) and genuine break-evens were all shown as
losses, and total P&L / win rate were off.

Each row is classified once:
  rejected_entry  ERROR whose message starts "Entry leg": Dhan never filled the buy (no shares held)
  error           any other ERROR row: not a settled trade, excluded from P&L and win rate
  pending         exit price still the entry placeholder (message carries *_PENDING_RECONCILE or
                  *_UNRESOLVED): P&L unknown, excluded from P&L and win rate until resolved
  win / loss      resolved trade with P&L > 0 / < 0
  breakeven       resolved trade with P&L == 0 (within half a paisa)
OPEN rows and rows with no P&L are ignored. Win rate = wins / resolved trades (breakevens stay in
the denominator, so a flat exit is not hidden).
"""
from __future__ import annotations

from typing import Iterable, Optional

_EPS = 0.005


def classify(row) -> Optional[str]:
    status = getattr(row, "status", None)
    msg = getattr(row, "error_message", None) or ""
    if status == "ERROR":
        return "rejected_entry" if msg.startswith("Entry leg") else "error"
    if status in (None, "OPEN", "EXIT_LEGS_REJECTED"):
        return None
    if "_PENDING_RECONCILE" in msg or "_UNRESOLVED" in msg:
        return "pending"
    pnl = getattr(row, "realized_pnl", None)
    if pnl is None:
        return None
    if pnl > _EPS:
        return "win"
    if pnl < -_EPS:
        return "loss"
    return "breakeven"


def is_settled(row) -> bool:
    return classify(row) in ("win", "loss", "breakeven")


def summarize(rows: Iterable) -> dict:
    rows = list(rows)
    buckets: dict[str, list] = {k: [] for k in ("win", "loss", "breakeven", "pending", "error", "rejected_entry")}
    for r in rows:
        k = classify(r)
        if k:
            buckets[k].append(r)
    settled = buckets["win"] + buckets["loss"] + buckets["breakeven"]
    total_pnl = sum(r.realized_pnl for r in settled)
    best = max(settled, key=lambda r: r.realized_pnl) if settled else None
    worst = min(settled, key=lambda r: r.realized_pnl) if settled else None
    return {
        "total_trades": len(settled),
        "wins": len(buckets["win"]),
        "losses": len(buckets["loss"]),
        "breakeven": len(buckets["breakeven"]),
        "pending_reconcile": len(buckets["pending"]),
        "error_trades": len(buckets["error"]),
        "rejected_entries": len(buckets["rejected_entry"]),
        "win_rate_pct": round(100.0 * len(buckets["win"]) / len(settled), 1) if settled else None,
        "total_pnl": round(total_pnl, 2),
        "best": best,
        "worst": worst,
    }
