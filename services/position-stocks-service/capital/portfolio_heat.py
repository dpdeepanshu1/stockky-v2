"""portfolio_heat.py - pure helpers for the shared portfolio-heat limit (E276 round 50). Identical copy in both services (see scripts/check_shared_copies.py).

Heat = rupees that would be lost if every open stop were hit, plus a gap allowance for positions carried overnight (a stop can gap), as a % of the pool.
No I/O, no imports from the services, never raises: bad numbers count as 0."""
from __future__ import annotations

from typing import Optional, Sequence, Tuple


def _f(x, d: float = 0.0) -> float:
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def stop_risk(entry, stop, qty, side: str = "LONG") -> float:
    """Rupees lost if the stop fills at its price. A stop at or past breakeven (long: stop >= entry) risks nothing."""
    e, s, q = _f(entry), _f(stop, _f(entry)), max(0.0, _f(qty))
    d = (e - s) if str(side).upper() in ("LONG", "BUY") else (s - e)
    return max(0.0, d) * q


def gap_allowance(entry, qty, gap_pct) -> float:
    """Extra rupees a carried position can lose beyond its stop when the open gaps through it: entry x qty x gap_pct %."""
    return max(0.0, _f(entry)) * max(0.0, _f(qty)) * max(0.0, _f(gap_pct)) / 100.0


def position_heat(entry, stop, qty, side: str = "LONG", carried: bool = False, gap_pct=0.0) -> Tuple[float, float]:
    """(stop risk, gap allowance) in rupees for one position. The allowance is 0 unless the position is carried overnight."""
    return stop_risk(entry, stop, qty, side), (gap_allowance(entry, qty, gap_pct) if carried else 0.0)


def p95_gap_pct(opens: Sequence, closes: Sequence, min_days: int = 20) -> Optional[float]:
    """95th percentile of the adverse overnight gap (a gap DOWN from the previous close, in %, long side) over aligned daily opens/closes.
    None when fewer than `min_days` usable pairs (the caller then uses its default)."""
    gaps = []
    for i in range(1, min(len(opens), len(closes))):
        pc, o = _f(closes[i - 1]), _f(opens[i])
        if pc > 0 and o > 0:
            gaps.append(max(0.0, (pc - o) / pc * 100.0))
    if len(gaps) < int(min_days):
        return None
    gaps.sort()
    k = min(len(gaps) - 1, int(round(0.95 * (len(gaps) - 1))))
    return round(gaps[k], 3)


def heat_pct(total_risk, pool) -> float:
    p = _f(pool)
    return (max(0.0, _f(total_risk)) / p * 100.0) if p > 0 else 0.0


def sector_key(sector, side: str = "LONG") -> str:
    """Key of one sector on one side, e.g. "Banks:LONG". A long and a short in the same sector hedge each other, so they are counted apart."""
    s = "SHORT" if str(side).upper() in ("SHORT", "SELL") else "LONG"
    return f"{str(sector or '').strip()}:{s}"


def sector_surcharge(same_sector_risk, new_risk, mult=1.5) -> float:
    """E276 round 60: extra rupees counted when a new entry lands in a sector that already holds an open position on the same side.
    The plan's rule: two positions in one sector count `mult` (1.5) times, so the surcharge is (mult - 1) x (the existing same-sector open risk + the new risk).
    0 when nothing risky is open there yet, when mult <= 1, or on bad numbers (the unlisted "Other" bucket is not a sector; the callers skip it)."""
    ex, nw, m = max(0.0, _f(same_sector_risk)), max(0.0, _f(new_risk)), _f(mult, 1.0)
    if ex <= 0 or nw <= 0 or m <= 1.0:
        return 0.0
    return (m - 1.0) * (ex + nw)


def merge_sector_risk(*maps) -> dict:
    """Add several {sector_key: rupees} maps together. Bad entries are skipped. Never raises."""
    out: dict = {}
    for m in maps:
        try:
            for k, v in (m or {}).items():
                out[str(k)] = out.get(str(k), 0.0) + max(0.0, _f(v))
        except Exception:  # noqa: BLE001
            continue
    return out


def check(own_risk, own_gap, peer_risk, new_risk, own_pool, peer_pool, limit_pct, sector_extra=0.0) -> Tuple[bool, float, float]:
    """(ok, heat % after the new entry, cap in rupees). The pool is the shared one (own + peer) when the peer published, else the own pool alone,
    which is the old single-service cap. limit_pct <= 0 = no limit. sector_extra (E276 round 60, default 0 = the old maths) is the same-sector surcharge
    from sector_surcharge(), added to the total."""
    pool = max(0.0, _f(own_pool)) + max(0.0, _f(peer_pool))
    cap = pool * _f(limit_pct) / 100.0
    total = max(0.0, _f(own_risk)) + max(0.0, _f(own_gap)) + max(0.0, _f(peer_risk)) + max(0.0, _f(new_risk)) + max(0.0, _f(sector_extra))
    if _f(limit_pct) <= 0 or pool <= 0:
        return True, heat_pct(total, pool), cap
    return total <= cap + 1e-9, heat_pct(total, pool), cap
