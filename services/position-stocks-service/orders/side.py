"""
orders/side.py - one place that knows which way a position faces.

Short selling, Part 2 step 3 (exit side). A LONG opens with a BUY and closes with a SELL;
a SHORT opens with a SELL and closes with a BUY. Every exit path asks this module instead
of hard-coding "SELL", and every P&L formula goes through pnl_per_share so a short books
(entry - exit), not (exit - entry).

Legacy rows (side NULL / "" / "LONG") behave exactly as before. An unknown side value
raises ValueError: closing a position the wrong way would DOUBLE it, so the exit path must
fail loudly (position stays OPEN, alert fires) rather than guess.

Nothing opens a SHORT yet (the short switch cannot be live yet), so this changes no behaviour
today; it makes sure the way OUT exists before the way IN is built. Stdlib only.
"""
from __future__ import annotations

SIDE_LONG = "LONG"
SIDE_SHORT = "SHORT"


def position_side(pos) -> str:
    raw = getattr(pos, "side", None)
    s = str(raw).strip().upper() if raw is not None else ""
    if s in ("", SIDE_LONG):
        return SIDE_LONG
    if s == SIDE_SHORT:
        return SIDE_SHORT
    raise ValueError(f"unknown position side {raw!r} (id={getattr(pos, 'id', None)})")


def is_short(pos) -> bool:
    return position_side(pos) == SIDE_SHORT


def entry_transaction_type(pos) -> str:
    return "SELL" if is_short(pos) else "BUY"


def exit_transaction_type(pos) -> str:
    """The order side that CLOSES this position."""
    return "BUY" if is_short(pos) else "SELL"


def pnl_per_share(pos, entry_price: float, exit_price: float) -> float:
    d = float(exit_price) - float(entry_price)
    return -d if is_short(pos) else d


def pnl(pos, entry_price: float, exit_price: float, quantity) -> float:
    return pnl_per_share(pos, entry_price, exit_price) * quantity


def pnl_pct(pos, entry_price: float, exit_price: float) -> float:
    if not entry_price:
        return 0.0
    return pnl_per_share(pos, entry_price, exit_price) / float(entry_price) * 100.0


def target_hit_at(pos, price: float, target: float, tol: float = 0.002) -> bool:
    """True when `price` is at/through the target (within tol). Long: price >= target; short: price <= target."""
    if not target:
        return False
    return price <= target * (1 + tol) if is_short(pos) else price >= target * (1 - tol)


def can_carry_overnight(pos) -> bool:
    """Shorts are never carried: intraday only, and a CNC conversion / CNC stop cannot exist for a short."""
    return not is_short(pos)
