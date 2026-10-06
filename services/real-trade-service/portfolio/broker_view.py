"""Normalised view of Dhan's /positions payload for the dashboard (group 208, item 13).

Dhan's positions endpoint carries no last-traded price, and its closed positions stay in the list with
netQty 0. The dashboard read `lastTradedPrice || ltp || 0` (always 0.00), fell back to the day BUY value
when `unrealizedProfit` was 0 (so "P&L" equalled the cost of the position), and listed closed positions
as open. This module turns each raw row into explicit fields: only open rows are `is_open`, a missing LTP
is None (shown as a dash), and unrealised P&L is None unless it can be computed or Dhan supplied it.

Pure functions, no I/O."""
from __future__ import annotations

from typing import Any, Mapping, Optional


def _num(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _first(row: Mapping, *keys: str) -> Optional[float]:
    for k in keys:
        n = _num(row.get(k))
        if n is not None:
            return n
    return None


def net_qty(row: Mapping) -> float:
    n = _num(row.get("netQty"))
    if n is not None:
        return n
    # No netQty field at all: derive it from the day's legs rather than guessing "open".
    b, s = _num(row.get("buyQty")), _num(row.get("sellQty"))
    return (b or 0.0) - (s or 0.0) if (b is not None or s is not None) else 0.0


def symbol_of(row: Mapping) -> str:
    return str(row.get("tradingSymbol") or row.get("symbol") or "").strip()


def open_symbols(raw: list) -> list[str]:
    """Distinct symbols that still hold shares (the only ones worth quoting)."""
    out = []
    for r in raw or []:
        sym = symbol_of(r)
        if sym and net_qty(r) != 0 and sym not in out:
            out.append(sym)
    return out


def normalize(raw: list, ltps: Optional[Mapping[str, float]] = None) -> list[dict]:
    """One dict per raw row. `ltps`: symbol -> last price (missing symbols simply have no LTP)."""
    ltps = ltps or {}
    rows = []
    for r in raw or []:
        sym = symbol_of(r)
        nq = net_qty(r)
        is_open = nq != 0
        long_side = nq > 0
        avg = (_first(r, "buyAvg", "costPrice", "averageBuyPrice") if long_side
               else _first(r, "sellAvg", "costPrice", "averageSellPrice")) if is_open else None
        ltp = _num(ltps.get(sym)) if is_open else None
        if ltp is not None and ltp <= 0:
            ltp = None
        unreal = None
        if is_open and ltp is not None and avg:
            unreal = round((ltp - avg) * nq, 2)            # works for shorts too (nq < 0)
        elif is_open:
            unreal = _num(r.get("unrealizedProfit"))        # Dhan's own figure, only if it sent one
        rows.append({
            "symbol": sym,
            "product": r.get("productType") or r.get("positionType") or "",
            "net_qty": nq,
            "is_open": is_open,
            "avg_price": avg,
            "ltp": ltp,
            "unrealized_pnl": unreal,
            "realized_pnl": _num(r.get("realizedProfit")),
            "cost_value": round(abs(nq) * avg, 2) if (is_open and avg) else None,
        })
    return rows
