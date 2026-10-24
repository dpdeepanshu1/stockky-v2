"""orders/short_entry.py - the SHADOW short entry (short selling step 5, 2026-10-23). Position Stocks only.

WHAT: when the short mode is `shadow`, every cycle scans for fallers (screening.engine.scan_shorts), walks the mirrored fail-closed gates
(orders/short_gates.py) and records each short Stockky WOULD have taken as a `scalp_short_shadow` row. Each row is then followed with live ticks to its stop,
target or the short square-off time, so the shadow result is honest (stop first when both would be hit; a stop is booked at the price actually seen,
a target at the target level; charges estimated). The result feeds the S2 gate of the plan: 4 weeks and 100 would-be shorts, positive after charges.

WHAT IT NEVER DOES: place or cancel an order, reserve capital, claim a symbol lock or spend order budget. No broker module is touched here, and the long
`attempt_entry` is not edited. With the mode `off` it only closes rows left over from an earlier switch-on. Everything here swallows its own errors.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

import config
from control.kit import current_profile as _current_profile
from capital import ledger
from models import ScalpCandidateLog, ScalpPosition, ScalpShortShadow
from orders import calendar_gate, cost_gate, depth_gate, entry_pause, rr_floor, short_gates, sizing, stopout_guard
from orders.adaptive import compute as compute_levels
from tz_utils import ist_time_at_or_after, ist_today_str, parse_hhmm

logger = logging.getLogger("position-stocks-short-entry")

_SKIP_LOG_EVERY_S = 300.0
_skip_logged: Dict[str, float] = {}
_last_shortlist: Dict[str, Any] = {"at": None, "mode": None, "enabled": None, "rows": []}     # E107: the latest ranked short shortlist, for GET /short/shadow
_stats: Dict[str, Any] = {"cycles": 0, "scanned": 0, "recorded": 0, "refused": 0, "by_code": {}}


def mode() -> str:
    """off | shadow. Anything else, including a `live` value, reads as off: nothing here may ever place an order."""
    m = str(getattr(config, "SHORT_MODE", "off") or "off").strip().lower()
    return "shadow" if m == "shadow" else "off"


def cfg_mode() -> str:
    """off | shadow | live as configured. mode() above is the SHADOW view (live reads as off there): nothing in this file may ever place an order; only the cycle
    uses this to hand candidates to orders/short_live.py, which re-checks live itself."""
    m = str(getattr(config, "SHORT_MODE", "off") or "off").strip().lower()
    return m if m in ("shadow", "live") else "off"


def stop_mult_for_state(state: Optional[str]) -> float:
    """Step 12: the fraction of a normal short stop to use in this Market Weather state. stressed / panic come from config (0.3 .. 1.0); every other state and any
    unreadable value is 1.0 (no change). Never above 1.0: this can only TIGHTEN a stop."""
    st = str(state or "").strip().lower()
    try:
        if st == "stressed":
            v = float(config.SHORT_STOP_MULT_STRESSED)
        elif st == "panic":
            v = float(config.SHORT_STOP_MULT_PANIC)
        else:
            return 1.0
    except Exception:  # noqa: BLE001
        return 1.0
    if v != v:
        return 1.0
    return min(1.0, max(0.3, v))


def tighten_short_levels(levels: Any, live: float, mult: float) -> Any:
    """Move a SHORT's stop closer to `live` (stop distance x mult), re-rounded to the tick and kept above `live` and never wider than before; stop_pct follows.
    mult >= 1 (or a result that would be on the wrong side of price) returns the levels untouched. The target is not moved. Returns the same object, edited."""
    if mult >= 1.0 or not (live > 0):
        return levels
    from orders.adaptive import round_to_tick
    new_pct = float(levels.stop_pct) * mult
    px = round_to_tick(live * (1 + new_pct / 100.0))
    if px <= live or px >= float(levels.stop_price):         # never on the wrong side of price and never WIDER than the stop we were given
        return levels
    levels.stop_price = px
    levels.stop_pct = (px - live) / live * 100.0
    return levels


def reset() -> None:
    _skip_logged.clear()
    _stats.clear()
    _stats.update(cycles=0, scanned=0, recorded=0, refused=0, by_code={})


def _count(code: str) -> None:
    _stats["refused"] = _stats.get("refused", 0) + 1
    bc = _stats.setdefault("by_code", {})
    bc[code] = bc.get(code, 0) + 1


def _log(db: Session, cand: Any, decision: str, reason: str) -> None:
    try:
        db.add(ScalpCandidateLog(symbol=cand.symbol, window_source=str(getattr(cand, "window_label", "") or "")[:8],
                                 pct_change=float(getattr(cand, "pct_change", 0.0) or 0.0),
                                 composite_score=getattr(cand, "composite_score", None), decision=decision, reason=reason))
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("short shadow: candidate log write failed (%s)", e)
        try:
            db.rollback()
        except Exception as _e:  # noqa: BLE001
            logger.debug("short_entry.py: rollback ignored %s", _e)


def _refuse(db: Session, cand: Any, code: str, reason: str) -> None:
    """Count every refusal; write one SHORT_SKIP row per symbol per 5 minutes so the candidate log is not flooded."""
    _count(code)
    now = time.monotonic()
    last = _skip_logged.get(cand.symbol)
    if last is not None and now - last < _SKIP_LOG_EVERY_S:
        return
    _skip_logged[cand.symbol] = now
    _log(db, cand, "SHORT_SKIP", f"{code}:{reason}"[:900])


def _live_price(symbol: str) -> Optional[float]:
    try:
        from feed import ws_client
        buf = ws_client.get_tick_buffer(symbol)
        if buf and buf[-1][1] and buf[-1][1] > 0:
            return float(buf[-1][1])
    except Exception as e:  # noqa: BLE001
        logger.debug("short shadow: no live price for %s (%s)", symbol, e)
    return None


def _short_position_value(db: Session, stop_pct: float) -> Optional[float]:
    """The rupee budget a REAL short would get: the same formula as capital/ledger.reserve_capital (risk % x market-state x short multiplier / slots
    / stop), without reserving anything. None when the pool is unknown or too small."""
    row = ledger._get_or_create(db)
    if not row or row.total_allocated_capital <= 0 or not (stop_pct > 0):
        return None
    risk = (row.total_allocated_capital * (config.RISK_PER_TRADE_PCT / 100.0) * ledger._market_state_risk_mult()
            * ledger._short_risk_mult("SHORT") / max(1, config.MAX_CONCURRENT_SCALP_POSITIONS))
    value = risk / (stop_pct / 100.0)
    cap_mult = float(getattr(config, "SCALP_SLOT_NOTIONAL_CAP_MULT", 0.0) or 0.0)
    if cap_mult > 0:
        value = min(value, row.total_allocated_capital / max(1, config.MAX_CONCURRENT_SCALP_POSITIONS) * cap_mult)
    if value > row.available_capital:
        return None
    return value


def attempt_short_shadow(db: Session, cand: Any, quality: Any = None, board_row: Optional[dict] = None,
                         now: Optional[datetime] = None) -> Optional[ScalpShortShadow]:
    """Record one would-be short, or refuse it (counted, and logged once per 5 min). Returns the shadow row or None. Never raises, never orders."""
    if mode() != "shadow":
        return None
    try:
        return _attempt(db, cand, quality, board_row, now)
    except Exception as e:  # noqa: BLE001
        logger.warning("short shadow: attempt for %s failed (%s: %s)", getattr(cand, "symbol", "?"), type(e).__name__, e)
        try:
            db.rollback()
        except Exception as _e:  # noqa: BLE001
            logger.debug("short_entry.py: rollback ignored %s", _e)
        return None


def _attempt(db: Session, cand: Any, quality: Any, board_row: Optional[dict], now: Optional[datetime]) -> Optional[ScalpShortShadow]:
    sym = cand.symbol
    day = ist_today_str(now)

    def _shadow_only() -> bool:
        """The two checks that only concern shadow rows. Run at the same point of the gate order as before the split (after the held check)."""
        if db.query(ScalpShortShadow).filter(ScalpShortShadow.symbol == sym).filter(ScalpShortShadow.day == day).first() is not None:
            _count("SHORT_ALREADY_RECORDED")                      # one shadow per symbol per day; not worth a log row
            return True
        n_open = len(db.query(ScalpShortShadow).filter(ScalpShortShadow.status == "OPEN").filter(ScalpShortShadow.day == day).all())
        if n_open >= int(config.SHORT_MAX_POSITIONS):
            _refuse(db, cand, "SHORT_MAX_POSITIONS", f"{n_open} unresolved shadow shorts (max {config.SHORT_MAX_POSITIONS})")
            return True
        return False

    plan = plan_short(db, cand, quality, board_row, now, _shadow_only)
    if plan is None:
        return None
    live, levels, qty, conv = plan["live"], plan["levels"], plan["qty"], plan["conv"]
    row = ScalpShortShadow(day=day, symbol=sym, window_source=str(getattr(cand, "window_label", "") or "")[:8], pct_change=float(cand.pct_change),
                           entry_price=live, stop_price=float(levels.stop_price), target_price=float(levels.target_price),
                           stop_pct=float(levels.stop_pct), target_pct=float(levels.target_pct), quantity=int(qty), position_value=round(qty * live, 2),
                           status="OPEN", low_water=live, high_water=live, board_conviction=(float(conv) if conv is not None else None),
                           market_state=(_current_profile() or {}).get("market_state"))
    db.add(row)
    db.commit()
    _stats["recorded"] = _stats.get("recorded", 0) + 1
    _log(db, cand, "SHORT_SHADOW", f"would short {qty} @ {live:.2f} stop {row.stop_price:.2f} target {row.target_price:.2f} (shadow, no order)")
    return row


def plan_short(db: Session, cand: Any, quality: Any, board_row: Optional[dict], now: Optional[datetime],
               extra_refuse: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """The mirrored gate walk, levels and sizing of ONE short candidate, shared by the shadow row and the live entry (orders/short_live.py), so the two can
    never drift apart. Returns {"live", "levels", "qty", "conv", "budget_stop_pct"} or None when refused (counted and logged once per 5 min). `extra_refuse()` is called
    once after the held-symbol check and returns True when the caller refused (it does its own counting). Places nothing, reserves nothing."""
    sym, ltp = cand.symbol, float(cand.current_ltp)
    for fn in (lambda: entry_pause.all_paused(), lambda: entry_pause.symbol_blocked(sym), lambda: stopout_guard.entry_reason(db, sym),
               lambda: calendar_gate.entry_block_reason(now), lambda: calendar_gate.scalp_cutoff_reason(now)):
        why = fn()
        if why:
            _refuse(db, cand, "SHORT_GUARD", str(why))
            return None
    if ledger._market_state_short_mult() <= 0.0:
        # step 6: the Market Weather short dial is 0 today (calm / choppy market, or unreadable). Nothing downstream may size a short.
        _refuse(db, cand, "SHORT_MARKET_STATE_OFF", f"Market Weather: no shorts today (state {(_current_profile() or {}).get('market_state') or 'unknown'}, short dial 0)")
        return None
    if ltp < float(config.MIN_STOCK_PRICE or 0.0) or ltp < float(getattr(config, "ENTRY_MIN_PRICE", 0.0) or 0.0):
        _refuse(db, cand, "SHORT_PENNY_STOCK", f"ltp {ltp:.2f} below the price floor")
        return None
    held = (db.query(ScalpPosition).filter(ScalpPosition.symbol == sym)
            .filter(ScalpPosition.status.in_(("OPEN", "EXIT_LEGS_REJECTED"))).first())
    if held is not None:
        _refuse(db, cand, "SHORT_ALREADY_HELD", f"{sym} is an open position here")
        return None
    if extra_refuse is not None and extra_refuse():
        return None

    facts = short_gates.gather_facts(sym, ltp, board_row, quality, now)
    verdict = short_gates.evaluate(facts, now)
    if verdict:
        _refuse(db, cand, verdict[0], verdict[1])
        return None

    live = float(facts.get("live") or ltp)
    levels = compute_levels(cand.pct_change, live, symbol=sym, side="SHORT")
    budget_stop_pct = float(levels.stop_pct)                 # size is worked out from the NORMAL stop (step 12): a tighter stop lowers risk, never grows the position
    mult = stop_mult_for_state((_current_profile() or {}).get("market_state"))
    if mult < 1.0:
        tighten_short_levels(levels, live, mult)
    why = rr_floor.geometry_reason(levels, live) or rr_floor.reject_reason(levels)
    if why:
        _refuse(db, cand, "SHORT_LEVELS", why)
        return None
    from screening import opening_gate
    why = opening_gate.stop_reject(sym, levels.stop_pct, now)
    if why:
        _refuse(db, cand, "SHORT_OPENING_STOP", why)
        return None

    value = _short_position_value(db, budget_stop_pct)
    if value is None:
        _refuse(db, cand, "SHORT_NO_CAPITAL", "the short budget does not fit the available pool")
        return None
    qty = sizing.compute_quantity(value, live, "SHORT")
    if qty is None:
        _refuse(db, cand, "SHORT_ONE_SHARE_TOO_BIG", f"one share at {live:.2f} exceeds the budget {value:.2f}")
        return None
    for cap_fn in (lambda: depth_gate.max_qty_from_book(sym, live), lambda: depth_gate.max_qty_from_depth20(sym, qty)):
        cap = cap_fn()
        if cap is not None and qty > cap:
            qty = cap
    why = cost_gate.reject_reason(live, qty, levels.target_pct)
    if why:
        _refuse(db, cand, "SHORT_COST_GATE", str(why))
        return None

    conv = (board_row or {}).get("conviction") if isinstance(board_row, dict) else None
    return {"live": live, "levels": levels, "qty": int(qty), "conv": conv, "budget_stop_pct": budget_stop_pct}


def _close(r: ScalpShortShadow, status: str, exit_price: float, now: Optional[datetime]) -> None:
    qty = int(r.quantity or 0)
    gross = (float(r.entry_price) - exit_price) * qty
    try:
        # a short sells first and buys back later: the buy leg is the EXIT, the sell leg the entry
        levies = cost_gate.estimate_levies(exit_price, qty, float(r.entry_price)) if qty > 0 else 0.0
    except Exception as e:  # noqa: BLE001
        logger.debug("short shadow: levy estimate failed (%s)", e)
        levies = 0.0
    r.status, r.exit_price = status, round(exit_price, 2)
    r.gross_pnl, r.net_pnl = round(gross, 2), round(gross - levies, 2)
    base = float(r.entry_price) * qty
    r.net_pnl_pct = round((gross - levies) / base * 100.0, 3) if base > 0 else None
    r.closed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def resolve_open(db: Session, now: Optional[datetime] = None) -> int:
    """Follow every OPEN shadow row with the live price. Returns how many were closed. Never raises."""
    closed = 0
    try:
        today = ist_today_str(now)
        sq = parse_hhmm(str(getattr(config, "SHORT_SQUAREOFF_TIME_IST", "14:55")), 14, 55)
        for r in db.query(ScalpShortShadow).filter(ScalpShortShadow.status == "OPEN").all():
            try:
                if r.day != today:
                    r.status, r.closed_at = "EXPIRED", (now or datetime.now(timezone.utc))
                    closed += 1
                    continue
                px = _live_price(r.symbol)
                if px is None:
                    continue
                r.low_water = min(float(r.low_water or px), px)
                r.high_water = max(float(r.high_water or px), px)
                if px >= float(r.stop_price):
                    _close(r, "STOP", px, now)
                elif px <= float(r.target_price):
                    _close(r, "TARGET", float(r.target_price), now)
                elif ist_time_at_or_after(sq, now):
                    _close(r, "EOD", px, now)
                else:
                    continue
                closed += 1
            except Exception as e:  # noqa: BLE001
                logger.debug("short shadow: resolve of %s failed (%s)", getattr(r, "symbol", "?"), e)
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("short shadow: resolve failed (%s)", e)
        try:
            db.rollback()
        except Exception as _e:  # noqa: BLE001
            logger.debug("short_entry.py: rollback ignored %s", _e)
    return closed


def _row_dict(r: ScalpShortShadow) -> Dict[str, Any]:
    return {k: getattr(r, k, None) for k in ("id", "day", "symbol", "window_source", "pct_change", "entry_price", "stop_price", "target_price", "quantity",
                                             "status", "exit_price", "gross_pnl", "net_pnl", "net_pnl_pct", "low_water", "high_water", "board_conviction",
                                             "market_state")} | {"created_at": r.created_at.isoformat() if r.created_at else None}


def summary(db: Session, recent: int = 50) -> Dict[str, Any]:
    """Read-only: totals over closed (TARGET / STOP / EOD) rows, the S2 progress counters, the gate refusal counters and the latest rows."""
    rows: List[ScalpShortShadow] = db.query(ScalpShortShadow).order_by(ScalpShortShadow.id.desc()).all()
    done = [r for r in rows if r.status in ("TARGET", "STOP", "EOD")]
    wins = [r for r in done if (r.net_pnl or 0.0) > 0]
    days = sorted({r.day for r in done})
    return {"mode": cfg_mode(), "total": len(rows), "open": sum(1 for r in rows if r.status == "OPEN"), "closed": len(done),
            "target": sum(1 for r in rows if r.status == "TARGET"), "stop": sum(1 for r in rows if r.status == "STOP"),
            "eod": sum(1 for r in rows if r.status == "EOD"), "expired": sum(1 for r in rows if r.status == "EXPIRED"),
            "net_pnl": round(sum(r.net_pnl or 0.0 for r in done), 2), "gross_pnl": round(sum(r.gross_pnl or 0.0 for r in done), 2),
            "win_rate_pct": round(len(wins) / len(done) * 100.0, 1) if done else None, "days_with_results": len(days),
            "s2_progress": {"would_be_shorts": len(done), "needed": 100, "days": len(days), "weeks_needed": 4},
            "gate_counters": {k: v for k, v in _stats.items()}, "shortlist": dict(_last_shortlist), "recent": [_row_dict(r) for r in rows[:max(1, int(recent))]]}


def _bear_rows() -> Dict[str, dict]:
    """symbol -> Market Board bearish row (best effort, cached by the board itself)."""
    try:
        if not config.BOARD_ENABLED:
            return {}
        from screening import board
        payload = board.build(None, None, [])
        return {str(r["symbol"]): dict(r, side="bear") for r in (payload or {}).get("bearish") or [] if isinstance(r, dict) and r.get("symbol")}
    except Exception as e:  # noqa: BLE001
        logger.debug("short shadow: board read failed (%s)", e)
        return {}


async def run_short_cycle(db: Session, now: Optional[datetime] = None) -> Dict[str, Any]:
    """One short pass: follow the open shadow rows, then (shadow or live mode, inside the entry window, market open) look for new shorts: shadow mode records
    would-be shorts, live mode hands each candidate to orders/short_live.py (a real SELL Super Order, capped by SHORT_LIVE_QTY_CAP).
    Returns {"mode", "resolved", "recorded"} (recorded is None when no scan ran). Never raises, never orders."""
    cm = cfg_mode()
    out: Dict[str, Any] = {"mode": cm, "resolved": 0, "recorded": None}
    try:
        out["resolved"] = await asyncio.to_thread(resolve_open, db, now)     # follows leftover shadow rows in every mode
        if cm not in ("shadow", "live"):
            return out
        from tz_utils import is_market_open_ist
        if not is_market_open_ist(now, honor_session_proof=False) or short_gates.time_window({}, now):
            return out
        _stats["cycles"] = _stats.get("cycles", 0) + 1
        from screening import quality_gate
        from screening.engine import scan_shorts
        held = {p.symbol for p in db.query(ScalpPosition).filter(ScalpPosition.status.in_(("OPEN", "EXIT_LEGS_REJECTED"))).all()}
        cands = await asyncio.to_thread(scan_shorts, held)
        bear = await asyncio.to_thread(_bear_rows)
        best: Dict[str, Any] = {}
        for c in cands:
            if c.symbol in bear and c.symbol not in best:     # only names the board also ranks bearish can pass the board gate; skip the rest cheaply
                best[c.symbol] = c
        pool = max(1, int(config.QUALITY_GATE_TOP_N))
        if getattr(config, "SHORT_SHORTLIST_ENABLED", True):
            pool = max(pool, int(config.SHORT_SHORTLIST_MAX))      # E107: look at up to SHORT_SHORTLIST_MAX names so the best can be picked
        top = list(best.values())[:pool]
        _stats["scanned"] = _stats.get("scanned", 0) + len(cands)
        cache = quality_gate.get_cache_batch(db, [c.symbol for c in top]) if top else {}
        quals = await asyncio.gather(*(quality_gate.check(c.symbol, cached=cache.get(c.symbol)) for c in top), return_exceptions=True)
        recorded = 0
        from screening import shortlist as _shortlist
        ranked = _shortlist.build_short([(c, (None if isinstance(q, BaseException) else q)) for c, q in zip(top, quals)], bear)
        _last_shortlist.update(at=time.time(), mode=cm, enabled=bool(getattr(config, "SHORT_SHORTLIST_ENABLED", True)),
                               rows=[dict(r, outcome="NOT_TRIED") for r in _shortlist.public(ranked)])
        for c, q in ((r["cand"], r["quality"]) for r in ranked):
            if cm == "live":
                from orders import short_live
                got = await asyncio.to_thread(short_live.attempt_short_live, db, c, q, bear.get(c.symbol), now)
            else:
                got = await asyncio.to_thread(attempt_short_shadow, db, c, q, bear.get(c.symbol), now)
            if got is not None:
                recorded += 1
            for _r in _last_shortlist["rows"]:
                if _r["symbol"] == c.symbol:
                    _r["outcome"] = "ENTERED" if got is not None else "REFUSED"
        out["recorded"] = recorded
    except Exception as e:  # noqa: BLE001
        logger.warning("short shadow cycle failed (%s: %s)", type(e).__name__, e)
    return out
