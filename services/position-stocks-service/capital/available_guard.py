"""capital/available_guard.py - E371: an upper bound on the ledger's running `available_capital` while positions are open.

`available_capital` is a running ledger (reserve subtracts, release adds). It is only re-based from Dhan's balance when the pool is flat, so
a release applied twice, one credited for a position that never reserved, or a pool that shrank (withdrawal) can leave it ABOVE what the
pool can really spend while positions stay open, and new entries are then sized from money that is not there.

Bound: total_allocated_capital - sum(capital_risked of the open positions). In normal flow the running figure sits at or below it (the
pool total adds back only SCALP_POOL_COMMITTED_MARGIN_FRAC of the committed cost, the ledger takes the full cost off), so the bound only
bites on drift. Profit can legitimately lift the running figure a little above it, hence a tolerance (default 5% of the pool total).

It only ever LOWERS available_capital (the safe direction) and never raises it: capital stuck low after a failed release is not repaired here.
SCALP_AVAILABLE_CEILING_CLAMP=0 turns it off; read on every call. Pure functions, never raise.
"""
from __future__ import annotations

import math
import os


def enabled() -> bool:
    return (os.getenv("SCALP_AVAILABLE_CEILING_CLAMP") or "").strip().lower() not in ("0", "false", "no", "off")


def tolerance_pct() -> float:
    try:
        v = float((os.getenv("SCALP_AVAILABLE_DRIFT_TOLERANCE_PCT") or "").strip() or 5.0)
        return v if math.isfinite(v) and v >= 0 else 5.0
    except ValueError:
        return 5.0


def clamped_available(available, total, committed_full, tol_pct=None):
    """The value available_capital should hold: `available` unchanged unless it is above (total - committed_full) by more than tol_pct % of
    total, in which case that ceiling. Bad or non-finite input returns `available` unchanged."""
    try:
        a, t, c = float(available), float(total), float(committed_full)
        if not (math.isfinite(a) and math.isfinite(t) and math.isfinite(c)) or t <= 0 or c <= 0:
            return available
        tol = (tolerance_pct() if tol_pct is None else float(tol_pct)) / 100.0 * t
        ceiling = t - c
        return ceiling if a > ceiling + tol else available
    except Exception:  # noqa: BLE001
        return available
