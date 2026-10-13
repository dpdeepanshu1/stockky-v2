"""stockky_ms.prep - sanitise, segment at corporate actions, flag gap / circuit bars, label quality."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .ms_types import C, H, L, O, T, V, Q_INSUFFICIENT, Q_LIMITED, Q_OK, Q_STALE, finite
from .params import MSParams


def _valid(b: Sequence[float], tol_pct: float) -> bool:
    try:
        if len(b) < 5 or not all(finite(b[i]) for i in (T, O, H, L, C)):
            return False
        o, h, l, c = float(b[O]), float(b[H]), float(b[L]), float(b[C])
    except (TypeError, ValueError):
        return False
    if o <= 0 or h <= 0 or l <= 0 or c <= 0 or h < l:
        return False
    tol = max(h, 1e-9) * tol_pct / 100.0
    return not (c > h + tol or c < l - tol or o > h + tol or o < l - tol)


def prep(raw_bars: Sequence[Sequence[float]], p: MSParams, stale: bool = False) -> Dict[str, Any]:
    """Return {bars, gap_event, circuit, dq, quality}. `bars` is the clean, ascending, segmented list of 6-tuples."""
    dropped = 0
    cleaned: List[tuple] = []
    for b in raw_bars or []:
        if not _valid(b, p.bar_tolerance_pct):
            dropped += 1
            continue
        vol = float(b[V]) if len(b) > V and finite(b[V]) and float(b[V]) >= 0 else 0.0
        cleaned.append((float(b[T]), float(b[O]), float(b[H]), float(b[L]), float(b[C]), vol))
    cleaned.sort(key=lambda x: x[0])
    uniq: List[tuple] = []
    for b in cleaned:
        if uniq and uniq[-1][0] == b[0]:
            dropped += 1
            uniq[-1] = b
        else:
            uniq.append(b)
    history = len(uniq)
    ca_index: Optional[int] = None
    for k in range(1, len(uniq)):
        if abs(uniq[k][C] / uniq[k - 1][C] - 1.0) * 100.0 > p.corp_action_jump_pct:
            ca_index = k
    bars = uniq[ca_index:] if ca_index is not None else uniq
    gap_event = [False] + [abs(bars[i][C] / bars[i - 1][C] - 1.0) * 100.0 > p.gap_event_pct for i in range(1, len(bars))]
    circuit = [b[H] == b[L] for b in bars]
    reasons: List[str] = []
    n = len(bars)
    if ca_index is not None:
        reasons.append("corporate_action_discontinuity")
    if n >= p.full_bars:
        quality = Q_OK
    elif n >= p.min_bars:
        quality = Q_LIMITED
        reasons.append("limited_history")
    else:
        quality = Q_INSUFFICIENT
        reasons.append("insufficient_history")
    if stale:
        quality = Q_STALE
        reasons.append("stale_data")
    dq = {"history_bars": history, "bars_used": n, "bars_dropped": dropped, "ca_discontinuity": ca_index is not None,
          "ca_index": ca_index, "insufficient_history": n < p.min_bars, "stale": bool(stale), "reasons": reasons}
    return {"bars": bars, "gap_event": gap_event, "circuit": circuit, "dq": dq, "quality": quality}
