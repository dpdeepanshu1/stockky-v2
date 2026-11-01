"""orders/excursion.py — record each open trade's highest / lowest price (2026-10-02).

Why: several scalps ended near zero (stop moved to entry after a rise, price
fell back). Whether the 3-4% target is simply too far for this style can only
be answered with the peak each trade actually reached, which was never stored.
This keeps ScalpPosition.max_price_seen / min_price_seen current while a
position is OPEN, so max favourable / adverse excursion can be read straight
off /positions and /trades/history.

Runs from main.py's fast-reconcile loop. Read-only with respect to Dhan (no
orders), independent of the breakeven toggle, fail-open per position.
Ticks are taken from the in-memory buffer since the position opened, so a
spike between two polls is still captured (as long as it is in the buffer).
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from models import ScalpPosition
from tz_utils import as_aware

logger = logging.getLogger("position-stocks-excursion")


def _extremes_since(symbol: str, since_ts: float) -> Optional[Tuple[float, float]]:
    """(high, low) of buffered ticks at/after since_ts, or None if none."""
    from feed import ws_client
    hi = lo = None
    for ts, px in ws_client.get_tick_buffer(symbol):
        if px is None or px <= 0 or ts < since_ts:
            continue
        hi = px if hi is None or px > hi else hi
        lo = px if lo is None or px < lo else lo
    return None if hi is None else (hi, lo)


def _extremes_since_ts(symbol: str, since_ts: float) -> Optional[Tuple[float, float, float, float]]:
    """E275 round 37: (high, low, time of the first tick at that high, time of the first tick at that low) of buffered ticks at/after since_ts, or None."""
    from feed import ws_client
    hi = lo = hi_ts = lo_ts = None
    for ts, px in ws_client.get_tick_buffer(symbol):
        if px is None or px <= 0 or ts < since_ts:
            continue
        if hi is None or px > hi:
            hi, hi_ts = px, ts
        if lo is None or px < lo:
            lo, lo_ts = px, ts
    return None if hi is None else (hi, lo, hi_ts, lo_ts)


def stamp_peak(flow_json, hi_at=None, lo_at=None):
    """E275 round 37: flow_json text with {"peak": {"hi_at", "lo_at"}} (epoch seconds when the trade's highest / lowest price was first seen), only the keys given are
    replaced. Record only (setup_stats learns each setup's time-to-peak from it); returns `flow_json` unchanged on any problem. Never raises."""
    import json
    try:
        base = json.loads(flow_json) if isinstance(flow_json, str) and flow_json else (dict(flow_json) if isinstance(flow_json, dict) else {})
        if not isinstance(base, dict):
            return flow_json
        pk = dict(base.get("peak")) if isinstance(base.get("peak"), dict) else {}
        if hi_at is not None:
            pk["hi_at"] = round(float(hi_at), 1)
        if lo_at is not None:
            pk["lo_at"] = round(float(lo_at), 1)
        base["peak"] = pk
        return json.dumps(base, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001
        return flow_json


def recovered_dip_pct(ticks, entry: float, short: bool = False) -> Optional[float]:
    """E285 round 107: the deepest pullback, as % of the running peak, that the trade made from an IN-PROFIT peak and then RECOVERED from (price went on to a new high; for a
    short, a new low). `ticks` = (time, price) pairs in time order. A pullback still going on at the end is not counted: a trail exit ends in one, so counting it would only
    echo the trail's own distance. None when the trade was never in profit; 0.0 when it was but never dipped and recovered. Pure; never raises."""
    try:
        e = float(entry)
        if e <= 0:
            return None
        peak, dd, best, profit = e, 0.0, 0.0, False
        for _ts, px in ticks:
            px = float(px)
            if px <= 0:
                continue
            if (px < peak) if short else (px > peak):                 # a new best price: the dip of the previous episode, if any, was recovered
                if profit and dd > best:
                    best = dd
                peak, dd, profit = px, 0.0, True
            elif profit:
                d = ((px - peak) if short else (peak - px)) / peak * 100.0
                if d > dd:
                    dd = d
        return round(best, 4) if profit else None
    except Exception:  # noqa: BLE001
        return None


def stamp_dip(flow_json, dip_pct):
    """flow_json text with flow_json["peak"]["dip_pct"] raised to `dip_pct` (never lowered). Unchanged on any problem. Never raises."""
    import json
    try:
        base = json.loads(flow_json) if isinstance(flow_json, str) and flow_json else (dict(flow_json) if isinstance(flow_json, dict) else {})
        if not isinstance(base, dict):
            return flow_json
        pk = dict(base.get("peak")) if isinstance(base.get("peak"), dict) else {}
        old = pk.get("dip_pct")
        if isinstance(old, (int, float)) and old >= dip_pct:
            return flow_json
        pk["dip_pct"] = round(float(dip_pct), 4)
        base["peak"] = pk
        return json.dumps(base, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001
        return flow_json


def _stored_dip(flow_json) -> Optional[float]:
    import json
    try:
        d = json.loads(flow_json) if isinstance(flow_json, str) and flow_json else flow_json
        v = d.get("peak", {}).get("dip_pct") if isinstance(d, dict) else None
        return float(v) if isinstance(v, (int, float)) else None
    except Exception:  # noqa: BLE001
        return None


def run_excursion_tracking(db: Session) -> int:
    """Update max/min price seen for every OPEN position. Returns the number of
    positions whose stored high/low changed."""
    changed = 0
    for pos in db.query(ScalpPosition).filter(ScalpPosition.status == "OPEN").all():
        try:
            if not pos.entry_price or pos.entry_price <= 0:
                continue
            opened_ts = as_aware(pos.opened_at).timestamp() if pos.opened_at else 0.0
            ext = _extremes_since_ts(pos.symbol, opened_ts)
            if ext is None:
                continue
            hi, lo, hi_ts, lo_ts = ext
            # the entry fill itself is a price this trade has been at (at the open time)
            if pos.entry_price >= hi:
                hi, hi_ts = pos.entry_price, opened_ts
            if pos.entry_price <= lo:
                lo, lo_ts = pos.entry_price, opened_ts
            dirty = False
            new_hi_at = new_lo_at = None
            if pos.max_price_seen is None or hi > pos.max_price_seen:
                pos.max_price_seen = hi
                new_hi_at = hi_ts
                dirty = True
            if pos.min_price_seen is None or lo < pos.min_price_seen:
                pos.min_price_seen = lo
                new_lo_at = lo_ts
                dirty = True
            if new_hi_at is not None or new_lo_at is not None:
                pos.flow_json = stamp_peak(getattr(pos, "flow_json", None), new_hi_at, new_lo_at)
            try:                                   # E285 round 107: the deepest recovered dip (record only; the trail learns from it per setup)
                from feed import ws_client
                from orders import side as _side
                _t = [(ts, px) for ts, px in ws_client.get_tick_buffer(pos.symbol) if px is not None and px > 0 and ts >= opened_ts]
                _dip = recovered_dip_pct(_t, pos.entry_price, _side.is_short(pos))
                if _dip is not None:
                    _old = _stored_dip(getattr(pos, "flow_json", None))
                    if _old is None or _dip > _old + 1e-9:
                        pos.flow_json = stamp_dip(getattr(pos, "flow_json", None), _dip)
                        dirty = True
            except Exception as _e:  # noqa: BLE001
                logger.debug("excursion: dip for %s skipped: %s", pos.symbol, _e)
            if dirty:
                db.commit()
                changed += 1
        except Exception as e:
            db.rollback()
            logger.debug("excursion: %s (id=%s) skipped: %s", pos.symbol, pos.id, e)
    return changed


def excursion_pcts(pos) -> dict:
    """Max gain / max drawdown % vs entry for API output (None when unknown)."""
    e, hi, lo = pos.entry_price, pos.max_price_seen, pos.min_price_seen
    if not e or e <= 0 or hi is None or lo is None:
        return {"max_gain_pct": None, "max_drawdown_pct": None}
    return {
        "max_gain_pct": round((hi - e) / e * 100.0, 2),
        "max_drawdown_pct": round((lo - e) / e * 100.0, 2),
    }
