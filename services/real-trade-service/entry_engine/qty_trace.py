"""entry_engine/qty_trace.py - round 223: one log line when the risk engine cuts an entry's quantity.

6 Nov 2026: AMBUJACEM was sized 18 shares and the order went out for 7; ANGELONE 22 -> 9. The cut happens inside risk_evaluate (conviction
size multiplier, per-trade / portfolio / per-symbol caps), and its reason only went to trade_decisions.risk_verdict_reason, never to the log, so
the log showed 'sized down 20 -> 18' and then 'cost gate: qty 7' with nothing between them. Pure string builder; never raises.
"""
from __future__ import annotations

from typing import Optional


def cut_line(mode, symbol, proposed_qty, approved_qty, check_name, reason, size_mult=None) -> Optional[str]:
    """A log line when approved_qty < proposed_qty, else None."""
    try:
        p, a = int(proposed_qty or 0), int(approved_qty or 0)
        if p <= 0 or a <= 0 or a >= p:
            return None
        mult = f", conviction size x{float(size_mult):.2f}" if size_mult not in (None, 0) else ""
        why = (str(reason or "").strip() or "no reason given")[:300]
        return f"entry_engine: {mode} {symbol} risk engine cut qty {p} -> {a} ({check_name or 'sized_down'}{mult}): {why}"
    except Exception:  # noqa: BLE001
        return None
