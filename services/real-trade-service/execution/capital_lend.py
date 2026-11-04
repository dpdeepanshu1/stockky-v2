"""execution/capital_lend.py - Real Trade may use the part of Position Stocks' half that is sitting idle (E286 round 217). Pure, no I/O, never raises.

Until now the 50 / 50 split of the one Dhan account was fixed: Real Trade could spend only half of Dhan's free cash (equity_sync) and hold at most half of the account's total
in exposure (risk engine "capital_share_cap"), even when Position Stocks was using a fraction of its own half (4 Nov: JSWENERGY refused with Rs 9,038 free while Position Stocks
used Rs 2,599 of its Rs 9,611). That is idle money.

total      = Dhan free cash + this service's exposure + the peer's exposure
peer_idle  = max(0, (100 - share_pct) % of total - the peer's exposure)         what the peer is entitled to but not using
reserve    = peer_idle x reserve_frac                                           kept back for the peer (0.5: half of its idle allowance stays free for it; 1: all of it)
exposure cap  = share_pct % of total + peer_idle x (1 - reserve_frac)            never below the fixed share
spendable cash = max(share_pct % of free cash, free cash - reserve)             never above the free cash

The peer's own sizing is a share of Dhan's FREE cash, so whatever it does not use is exactly what is left for this service; the reserve keeps part of it. A missing or old peer
figure (the caller passes `peer_fresh=False`) or mode `off` = the fixed share, exactly as before. Note that even reserve_frac 1 is fairer than the old cash cap (half of the free cash): the peer keeps
its whole unused allowance and this service gets the rest of the free cash. CAPITAL_LEND_MODE=off is the only way back to the exact old numbers.
"""
from __future__ import annotations

import math
from typing import Optional


def _f(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def peer_idle(total, peer_exposure, share_pct) -> float:
    t, p, s = _f(total), _f(peer_exposure), _f(share_pct)
    if t is None or p is None or s is None or t <= 0:
        return 0.0
    return max(0.0, t * (100.0 - s) / 100.0 - max(0.0, p))


def exposure_cap(total, peer_exposure, share_pct, reserve_frac, peer_fresh: bool = True) -> Optional[float]:
    """The most exposure this service may hold, or None when the fixed share applies (peer figure not fresh / bad input)."""
    t, s, r = _f(total), _f(share_pct), _f(reserve_frac)
    if not peer_fresh or t is None or s is None or r is None or t <= 0:
        return None
    r = min(1.0, max(0.0, r))
    base = t * s / 100.0
    lent = peer_idle(t, peer_exposure, s) * (1.0 - r)
    return min(t, base + lent)


def spendable_cash(balance, own_exposure, peer_exposure, share_pct, reserve_frac, peer_fresh: bool = True) -> Optional[float]:
    """Cash this service may spend, or None when the fixed share applies."""
    b, o, p, s, r = _f(balance), _f(own_exposure), _f(peer_exposure), _f(share_pct), _f(reserve_frac)
    if not peer_fresh or None in (b, o, p, s, r) or b <= 0:
        return None
    r = min(1.0, max(0.0, r))
    total = b + max(0.0, o) + max(0.0, p)
    reserve = peer_idle(total, p, s) * r
    return min(b, max(b * s / 100.0, b - reserve))
