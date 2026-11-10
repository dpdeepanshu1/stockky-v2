"""execution/heat_state.py - E276 round 50: this service's open risk and gap allowance, and the peer's, for the shared portfolio-heat check.

own_heat(db, mode)  -> (open risk, gap allowance) over the held positions: stop distance x qty (a stop at or above entry counts 0); the gap allowance
                       only on a position carried past the day it opened (a swing hold or any position opened on an earlier IST day).
peer_heat(db, mode) -> (peer open risk incl. its gap allowance, peer capital base); (0, 0) for DEMO, with the switch off, or when the peer row is missing / stale.
publish(db, capital_base) -> writes this service's row for position-stocks-service.
Everything is fail-open: a DB error reads as zeros (the old single-service cap) and never raises."""
from __future__ import annotations

import logging
from typing import Tuple

import config
from execution import gap_cache, shared_heat
from risk_engine import portfolio_heat as ph
from tz_utils import ist_today_str

logger = logging.getLogger("real-trade-heat-state")


def _carried(p) -> bool:
    try:
        from swing import live as _sw
        if _sw.is_swing(p):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        ts = getattr(p, "opened_at", None)
        if ts is None:
            return False
        from tz_utils import ist_now
        from datetime import timezone as _tz
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_tz.utc)
        return ist_now(ts).strftime("%Y-%m-%d") < ist_today_str()
    except Exception:  # noqa: BLE001
        return False


def _stop_kw(p) -> dict:
    """E360: fallbacks for a position whose current_stop is missing or NaN: its own initial_stop_distance, else HEAT_MISSING_STOP_PCT % of the entry."""
    return {"stop_distance": getattr(p, "initial_stop_distance", None),
            "max_stop_pct": float(getattr(config, "HEAT_MISSING_STOP_PCT", 6.0) or 0.0)}


def gap_pct_for(symbol) -> float:
    """Gap % for a carried stock: its own cached p95 (floored at HEAT_GAP_MIN_PCT) when known, else HEAT_GAP_DEFAULT_PCT. Never blocks."""
    default = float(getattr(config, "HEAT_GAP_DEFAULT_PCT", 1.5))
    if not bool(getattr(config, "HEAT_GAP_P95_ENABLED", True)):
        return default
    v = gap_cache.peek(symbol)
    return max(float(getattr(config, "HEAT_GAP_MIN_PCT", 0.5)), float(v)) if v is not None else default


def _heat_strict(positions) -> Tuple[float, float]:
    """The maths of positions_heat, but a failure RAISES (E343: publish() must be able to tell "nothing at risk" from "could not read")."""
    risk = gap = 0.0
    carried_syms = []
    for p in positions:
        c = _carried(p)
        if c:
            carried_syms.append(getattr(p, "symbol", None))
        r, g = ph.position_heat(p.avg_entry_price, p.current_stop, p.qty_open, "LONG", c, gap_pct_for(getattr(p, "symbol", None)), **_stop_kw(p))
        risk += r
        gap += g
    if carried_syms and bool(getattr(config, "HEAT_GAP_P95_ENABLED", True)):
        try:
            gap_cache.warm([x for x in carried_syms if x])
        except Exception as e:  # noqa: BLE001 - the background fetch is an extra, never a reason to lose the figure
            logger.warning("heat_state: gap warm-up failed (flat default used): %s", e)
    return risk, gap


def positions_heat(positions) -> Tuple[float, float]:
    """(open risk, gap allowance) in rupees for an iterable of TradePosition-like rows. Never raises. Carried symbols without a cached p95 are queued for a
    background fetch (the flat default is used until it lands)."""
    risk = gap = 0.0
    try:
        carried_syms = []
        for p in positions:
            c = _carried(p)
            if c:
                carried_syms.append(getattr(p, "symbol", None))
            r, g = ph.position_heat(p.avg_entry_price, p.current_stop, p.qty_open, "LONG", c, gap_pct_for(getattr(p, "symbol", None)), **_stop_kw(p))
            risk += r
            gap += g
        if carried_syms and bool(getattr(config, "HEAT_GAP_P95_ENABLED", True)):
            gap_cache.warm([x for x in carried_syms if x])
    except Exception as e:  # noqa: BLE001
        logger.warning("heat_state: own heat failed (counted as 0): %s", e)
        return 0.0, 0.0
    return risk, gap


def own_heat(db, mode: str, positions=None) -> Tuple[float, float]:
    if mode != "REAL" or not bool(getattr(config, "PORTFOLIO_HEAT_SHARED_ENABLED", True)):
        return 0.0, 0.0
    try:
        if positions is None:
            from portfolio.portfolio import held_exposure_positions
            positions = held_exposure_positions(db, mode)
        return positions_heat(positions)
    except Exception as e:  # noqa: BLE001
        logger.warning("heat_state: could not read positions (counted as 0): %s", e)
        return 0.0, 0.0


def peer_heat(db, mode: str) -> Tuple[float, float]:
    if mode != "REAL" or not bool(getattr(config, "PORTFOLIO_HEAT_SHARED_ENABLED", True)):
        return 0.0, 0.0
    risk, base, _age = shared_heat.get_peer(db, float(getattr(config, "HEAT_PEER_MAX_AGE_S", 900.0)))
    return risk, base


def _sector_strict(positions) -> dict:
    """The maths of sector_risk, but a failure RAISES (E343, for publish())."""
    out: dict = {}
    from risk_engine import sector_map
    for p in positions:
        sec = sector_map.sector_of(getattr(p, "symbol", ""))
        if sec == "Other":
            continue
        r = ph.stop_risk(p.avg_entry_price, p.current_stop, p.qty_open, "LONG", **_stop_kw(p))
        if r > 0:
            k = ph.sector_key(sec, "LONG")
            out[k] = out.get(k, 0.0) + r
    return out


def sector_risk(positions) -> dict:
    """E276 round 60: {"<sector>:LONG": open stop risk in rupees} over held positions, for the same-sector rule. The unlisted "Other" bucket is left out. Never raises."""
    out: dict = {}
    try:
        from risk_engine import sector_map
        for p in positions:
            sec = sector_map.sector_of(getattr(p, "symbol", ""))
            if sec == "Other":
                continue
            r = ph.stop_risk(p.avg_entry_price, p.current_stop, p.qty_open, "LONG", **_stop_kw(p))
            if r > 0:
                k = ph.sector_key(sec, "LONG")
                out[k] = out.get(k, 0.0) + r
    except Exception as e:  # noqa: BLE001
        logger.warning("heat_state: sector split failed (counted as none): %s", e)
        return {}
    return out


def own_sector_risk(db, mode: str, positions=None) -> dict:
    if mode != "REAL" or not bool(getattr(config, "PORTFOLIO_HEAT_SHARED_ENABLED", True)) or not bool(getattr(config, "SECTOR_HEAT_ENABLED", True)):
        return {}
    try:
        if positions is None:
            from portfolio.portfolio import held_exposure_positions
            positions = held_exposure_positions(db, mode)
        return sector_risk(positions)
    except Exception as e:  # noqa: BLE001
        logger.warning("heat_state: could not read positions for the sector split (counted as none): %s", e)
        return {}


def peer_sector_risk(db, mode: str) -> dict:
    if mode != "REAL" or not bool(getattr(config, "PORTFOLIO_HEAT_SHARED_ENABLED", True)) or not bool(getattr(config, "SECTOR_HEAT_ENABLED", True)):
        return {}
    return shared_heat.get_peer_sectors(db, float(getattr(config, "HEAT_PEER_MAX_AGE_S", 900.0)))


def publish(db, capital_base: float) -> None:
    """Publish this service's REAL open risk, gap allowance and capital base. Fail-open, never raises."""
    try:
        if not bool(getattr(config, "PORTFOLIO_HEAT_SHARED_ENABLED", True)):
            return
        # E343: own_heat() / own_sector_risk() answer 0 when the positions cannot be read, which is right for this service's own check (the old single-service cap)
        # but WRONG to publish: the peer would take a fresh row of zeros for "nothing at risk" and undercount the shared heat until the next publish.
        # When the read fails nothing is published; the peer keeps the last figure until HEAT_PEER_MAX_AGE_S, then ignores it (the old cap).
        try:
            from portfolio.portfolio import held_exposure_positions
            positions = held_exposure_positions(db, "REAL")
            risk, gap = _heat_strict(positions)
        except Exception as e:  # noqa: BLE001
            logger.warning("heat_state: publish skipped, own open risk could not be read: %s", e)
            return
        shared_heat.publish_own(db, risk, gap, float(capital_base or 0.0))
        if bool(getattr(config, "SECTOR_HEAT_ENABLED", True)):
            try:
                sectors = _sector_strict(positions)
            except Exception as e:  # noqa: BLE001
                logger.warning("heat_state: sector split not published, it could not be read: %s", e)
                return
            shared_heat.publish_sectors(db, sectors)   # E276 round 60
    except Exception as e:  # noqa: BLE001
        logger.warning("heat_state: publish failed (non-fatal): %s", e)
