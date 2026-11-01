"""swing/live.py - E132 (plan steps S3-S5): the swing lane's MONEY side. Everything here is inert unless SWING_MODE=live (and, for REAL, SWING_LIVE_REAL_ENABLED=true).

  entries(db, repo, mode, armed)   ~09:25 IST: buy last night's PLANNED picks as CNC through manual_engine (risk engine, cash, symbol lock, order budget,
                                   Dhan error handling all apply), up to the free slots, with the swing value cap and a stop of ATR x mult below the entry.
  eod_holds(db, mode, positions)   ~15:00 IST (called by auto_pilot._eod_squareoff BEFORE the overnight-hold selector): which swing positions skip the
                                   square-off. Anything not kept is sold by the normal square-off. With SWING_MODE not live nothing is kept (a leftover swing
                                   position is flattened).
  after_scan(db, repo, scan)       evening, on the scan thread: link ledger rows to their positions, close them when the position closed, and ratchet each open
                                   position's stop to (close - stop %) from the day's real close (the replay's rule).
  pause_for(repo, mode)            the S5 auto-stop: 3 losing swing trades in a row, or an exit that lost more than 1.3 x its planned stop.
  overdue(position)                E138 backstop, asked by the exit engine on every tick: sells a swing position the 15:00 square-off failed to sell / never saw.
  eod_squareoff_on(db, mode)       E138: live entries refuse to start unless the gate's EOD square-off switch is on (it is what runs the 15:00 hold decision).
  wiring(db, repo, now)            E138: one read-only answer to "is every piece of the lane switched on and reachable?" for GET /swing/status.
Every function fails closed (no entry / no carry) and never raises into the schedule tick.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple

from swing import engine, lane

logger = logging.getLogger("real-trade-swing-live")


def _cfg(name: str, default: Any = None) -> Any:
    import config
    return getattr(config, name, default)


def hold_days(db: Any = None) -> int:
    """E283: the sessions a swing position may be held now: SWING_MAX_HOLD_DAYS, or a shorter hold the ledger's own closed winners support (swing/learned_hold.py). Never raises."""
    try:
        from swing import learned_hold
        return int(learned_hold.hold_days(db))
    except Exception:  # noqa: BLE001
        return int(_cfg("SWING_MAX_HOLD_DAYS", 5))


def is_swing(position: Any) -> bool:
    return (getattr(position, "entry_decision_label", None) or "").upper() == lane.LABEL


def swing_positions(db: Any, mode: str) -> List[Any]:
    import models
    return [p for p in db.query(models.TradePosition).filter(models.TradePosition.mode == mode,
                                                              models.TradePosition.status.in_(("OPEN", "PARTIALLY_CLOSED"))).all() if is_swing(p)]


def held_symbols(db: Any) -> List[str]:
    out: List[str] = []
    for m in ("DEMO", "REAL"):
        try:
            out += [p.symbol for p in swing_positions(db, m)]
        except Exception:  # noqa: BLE001
            logger.debug("swing: held symbols for %s unreadable", m, exc_info=True)
    return out


def _day(d: Any) -> Optional[str]:
    from tz_utils import as_aware, IST
    try:
        return as_aware(d).astimezone(IST).date().isoformat()
    except Exception:  # noqa: BLE001
        return None


# ───────────────────────────── auto-stop ─────────────────────────────

def pause_for(repo: Any, mode: str) -> Tuple[bool, str]:
    try:
        closed = [t for t in repo.trades(kind="LIVE", status="CLOSED", mode=mode, newest_first=True) if t.get("net_pct") is not None]
        closed.sort(key=lambda t: str(t.get("closed_at") or ""), reverse=True)
        res = repo.latest_run("resume", mode=mode, ok_only=False)
        resumed = res["created_at"].replace(tzinfo=None).isoformat() if res and res.get("created_at") is not None else None
        return lane.pause_state(closed, int(_cfg("SWING_PAUSE_AFTER_LOSSES", 3)), bool(_cfg("SWING_PAUSE_ON_GAP_STOP", True)),
                                float(_cfg("SWING_GAP_STOP_MULT", 1.3)), resumed)
    except Exception as e:  # noqa: BLE001 - unreadable history: do not trade
        return True, f"pause check failed ({type(e).__name__}): {str(e)[:80]}"


# ───────────────────────────── E138: gate switch, backstop, wiring ─────────────────────────────

def eod_squareoff_on(db: Any, mode: str) -> bool:
    """True when the gate row's EOD square-off switch is on (it runs auto_pilot._eod_squareoff and with it eod_holds). Unreadable / no row = False."""
    try:
        import models
        gate = db.query(models.TradeGateState).filter_by(mode=mode).first()
        return bool(gate is not None and getattr(gate, "eod_squareoff_enabled", False))
    except Exception:  # noqa: BLE001
        return False


def backstop_cutoff(now: Optional[datetime] = None) -> Any:
    """IST clock time after which the LAST session's position counts as missed by the square-off: 10 minutes after EOD_SQUAREOFF_TIME_IST, never later
    than 15:25 (the market closes 15:30)."""
    from datetime import time as _t, timedelta
    from tz_utils import parse_hhmm
    base = parse_hhmm(str(_cfg("EOD_SQUAREOFF_TIME_IST", "15:00")), 15, 0)
    mins = min(base.hour * 60 + base.minute + 10, 15 * 60 + 25)
    return _t(mins // 60, mins % 60)


EXTEND_EVENT = "SWING_EVENT_EXTEND"


def extra_sessions(position: Any) -> int:
    """E274 round 49: sessions added to this swing position's last session by a positive event (one SWING_EVENT_EXTEND row each, at most SWING_EVENT_EXTEND_MAX). 0 on any doubt."""
    try:
        if not bool(_cfg("SWING_EVENT_EXTEND_ENABLED", True)):
            return 0
        from sqlalchemy.orm import object_session
        import models
        db = object_session(position)
        if db is None or getattr(position, "id", None) is None:
            return 0
        n = db.query(models.TradePositionEvent).filter_by(position_id=position.id, event_type=EXTEND_EVENT).count()
        return max(0, min(int(n), int(_cfg("SWING_EVENT_EXTEND_MAX", 1)) * int(_cfg("SWING_EVENT_EXTEND_SESSIONS", 1))))
    except Exception:  # noqa: BLE001
        return 0


def maybe_extend(db: Any, position: Any, ltp: Any, sessions: int) -> int:
    """E274 round 49: on the LAST session, a position in profit with a fresh typed POSITIVE event of at least SWING_EVENT_EXTEND_MIN_STRENGTH gets SWING_EVENT_EXTEND_SESSIONS more session(s), once
    (SWING_EVENT_EXTEND_MAX times in all). Records one SWING_EVENT_EXTEND row (the only state; no column). Returns the sessions now added (0 = none). Never raises."""
    try:
        have = extra_sessions(position)
        if not bool(_cfg("SWING_EVENT_EXTEND_ENABLED", True)) or sessions < hold_days(db) + have:
            return have
        if have >= int(_cfg("SWING_EVENT_EXTEND_MAX", 1)) * int(_cfg("SWING_EVENT_EXTEND_SESSIONS", 1)):
            return have
        px = float(ltp) if ltp is not None else 0.0
        if px <= float(position.avg_entry_price or 0.0):
            return have
        from watchlist_engine.intraday_news import positive_event_any
        ev = positive_event_any(db, position.symbol)
        if not ev or float(ev.get("strength") or 0.0) < float(_cfg("SWING_EVENT_EXTEND_MIN_STRENGTH", 0.6)):
            return have
        import models
        add = int(_cfg("SWING_EVENT_EXTEND_SESSIONS", 1))
        for _ in range(add):
            db.add(models.TradePositionEvent(position_id=position.id, event_type=EXTEND_EVENT,
                                             detail=f"Last session extended by a positive {ev.get('type')} event (strength {float(ev.get('strength') or 0):.2f}, {ev.get('source')}); position in profit"[:400]))
        db.commit()
        return have + add
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0


def overdue(position: Any, now: Optional[datetime] = None) -> Optional[str]:
    """Reason to sell this swing position NOW (exit-engine backstop, see lane.backstop_verdict), or None. Never raises; any doubt = None."""
    try:
        if not is_swing(position):
            return None
        from tz_utils import ist_now, ist_today_str
        from entry_engine import calendar_gate as cg
        n = ist_now(now)
        today = n.date()
        opened = _day(getattr(position, "opened_at", None))
        if opened is None:
            return None
        od = date.fromisoformat(opened)
        sessions = lane.count_sessions(od, today, cg._is_trading_day)
        from sqlalchemy.orm import object_session
        sell, why = lane.backstop_verdict(sessions=sessions, max_hold=hold_days(object_session(position)) + extra_sessions(position), opened_day=od, today=today,
                                          past_cutoff=n.time() >= backstop_cutoff(), swing_live=lane.norm_mode(_cfg("SWING_MODE", "live")) == "live")
        return why if sell else None
    except Exception:  # noqa: BLE001
        logger.debug("swing backstop check failed (no sale)", exc_info=True)
        return None


def wiring(db: Any, repo: Any, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Read-only: is every piece the lane needs switched on and reachable? `problems` is empty when nothing would stop it. Never raises."""
    out: Dict[str, Any] = {"problems": [], "modes": {}}
    prob: List[str] = out["problems"]
    try:
        import models
        from tz_utils import ist_now
        from entry_engine import calendar_gate as cg
        n = ist_now(now)
        sw_mode = lane.norm_mode(_cfg("SWING_MODE", "live"))
        out["swing_mode"] = sw_mode
        try:
            from execution import auto_pilot as _ap
            t = getattr(_ap, "_schedule_task", None)
            out["schedule_loop_running"] = bool(t is not None and not t.done())
        except Exception:  # noqa: BLE001
            out["schedule_loop_running"] = False
        if sw_mode != "off" and not out["schedule_loop_running"]:
            prob.append("the auto-pilot schedule loop is not running: the evening scan and the morning entries are started from it")
        for m in ("DEMO", "REAL"):
            gate = db.query(models.TradeGateState).filter_by(mode=m).first()
            held = swing_positions(db, m)
            allowed = lane.live_allowed(sw_mode, m, bool(_cfg("SWING_LIVE_REAL_ENABLED", False)))
            info = {"gate_row": gate is not None, "armed": bool(gate is not None and gate.armed),
                    "eod_squareoff_on": bool(gate is not None and getattr(gate, "eod_squareoff_enabled", False)),
                    "edis_check_on": bool(getattr(gate, "edis_morning_check_enabled", True)) if gate is not None else bool(_cfg("EDIS_MORNING_CHECK_ENABLED", True)),
                    "live_entries_allowed": allowed, "open_swing_positions": len(held)}
            out["modes"][m] = info
            if allowed and not info["armed"]:
                prob.append(f"{m}: the gate is not armed: live swing entries are refused")
            if (allowed or held) and not info["eod_squareoff_on"]:
                prob.append(f"{m}: the gate's EOD square-off switch is off - it runs the 15:00 hold decision"
                            + (" (live entries are refused until it is on)" if allowed else "")
                            + ("; the open swing position(s) would only be sold by the exit-engine backstop or their stop" if held else ""))
            if m == "REAL" and (allowed or held) and not info["edis_check_on"]:
                prob.append("REAL: the eDIS morning check is off - a carried CNC position cannot be sold on a morning without the CDSL TPIN verification")
        out["broker_stop"] = {"all_positions_switch": bool(_cfg("BROKER_STOP_ENABLED", False)), "swing_switch": bool(_cfg("SWING_BROKER_STOP_ENABLED", False))}
        if sw_mode != "off":
            scan = engine.latest_scan(repo)
            out["newest_scan"] = scan["asof"] if scan else None
            if not scan:
                prob.append("no evening scan stored yet (it starts after SWING_SCAN_TIME_IST on a trading day, from the DEMO schedule tick)")
            else:
                want = cg._prev_trading_day(n.date()).isoformat()      # the newest scan may be today's (after the evening scan) or the previous session's
                if str(scan["asof"]) < want:
                    prob.append(f"newest evening scan is for {scan['asof']}, older than the last completed session {want}")
    except Exception as e:  # noqa: BLE001
        prob.append(f"wiring check itself failed: {type(e).__name__}: {str(e)[:100]}")
    return out


# ───────────────────────────── entries ─────────────────────────────

def _open_check(symbol: str) -> Dict[str, Any]:
    """E273 round 15: the opening check for one swing pick (blocking: runs in a thread). Fresh bars are fetched now (this pass runs once a day, so the
    background cache is not enough); no bars / stale bars / too few range bars = {"ok": None}, i.e. buy as before. Never raises."""
    try:
        from entry_engine import orb_bars
        bars = orb_bars.fetch_now(symbol) or orb_bars.get_bars(symbol)
        return orb_bars.open_check(bars, max_drop_pct=float(_cfg("SWING_OPEN_MAX_DROP_PCT", 0.15)), min_bars=int(_cfg("SWING_OPEN_MIN_BARS", 3)))
    except Exception:  # noqa: BLE001
        return {"ok": None, "why": "error"}


async def entries(db: Any, repo: Any, mode: str, gate_armed: bool) -> Dict[str, Any]:
    """The morning pass for one mode. At most once per IST day per mode (the run row is written BEFORE any order). Never raises."""
    out: Dict[str, Any] = {"mode": mode, "placed": 0, "skipped": []}
    try:
        if not lane.live_allowed(_cfg("SWING_MODE", "live"), mode, bool(_cfg("SWING_LIVE_REAL_ENABLED", False))):
            return {**out, "status": "not_live"}
        if not gate_armed:
            return {**out, "status": "not_armed"}
        if not eod_squareoff_on(db, mode):      # E138: no 15:00 hold decision without this switch, so no new carried position either (the run row is NOT used up)
            return {**out, "status": "eod_squareoff_off", "detail": "the gate's EOD square-off switch is off or unreadable: it runs the daily 15:00 hold decision, so no swing entry"}
        from tz_utils import ist_now, ist_today_str, ist_time_at_or_after, parse_hhmm, is_market_open_ist
        _t_cfg = parse_hhmm(str(_cfg("SWING_ENTRY_TIME_IST", "09:25")), 9, 25)
        _t_floor = parse_hhmm(str(_cfg("SWING_ENTRY_FLOOR_IST", "09:21")), 9, 21)
        if not is_market_open_ist() or not ist_time_at_or_after(max(_t_cfg, _t_floor)):     # E273 round 13: never before the floor, whatever the entry time says
            return {**out, "status": "too_early"}
        today = ist_today_str()
        if ist_time_at_or_after(parse_hhmm(str(_cfg("SWING_ENTRY_LAST_TIME_IST", "10:30")), 10, 30)):
            return {**out, "status": "too_late"}
        if repo.has_run("entry", today, mode, ok_only=False):
            return {**out, "status": "done_today"}
        repo.add_run("entry", today, mode, True, "entry pass started")          # at-most-once: written before any order
        td = date.fromisoformat(today)
        from entry_engine import calendar_gate as cg
        scan = engine.latest_scan(repo)
        if not scan or scan["asof"] != cg._prev_trading_day(td).isoformat():
            return {**out, "status": "stale_scan", "detail": f"newest scan {scan['asof'] if scan else None}, needs {cg._prev_trading_day(td).isoformat()}"}
        paused, why = pause_for(repo, mode)
        if paused:
            await _notify(f"⏸ *Swing lane paused — {mode}*\n{why}\nNo new swing entry. POST /swing/resume clears it.")
            return {**out, "status": "paused", "detail": why}
        if bool(_cfg("SWING_BLOCK_STRESSED", True)):
            from control.kit import current_profile
            st = current_profile().get("market_state")
            if st in ("stressed", "panic"):
                return {**out, "status": "market_state", "detail": f"Market Weather is {st}"}
        blk = cg.entry_block_reason() or cg.late_entry_block_reason()
        if blk:
            return {**out, "status": "calendar", "detail": blk}
        picks = sorted(repo.trades(kind="SHADOW", status="PLANNED", signal_day=scan["asof"]), key=lambda t: t.get("rank") or 1e9)
        open_pos = swing_positions(db, mode)
        held = {p.symbol for p in open_pos}
        _backed: Dict[str, Any] = {}
        if picks and bool(_cfg("SWING_EVENT_FIRST_ENABLED", True)):      # E274 round 47: event-backed picks first
            try:
                from watchlist_engine.intraday_news import positive_event_any
                _backed = {p["symbol"]: await asyncio.to_thread(positive_event_any, db, p["symbol"]) for p in picks}
                picks = lane.order_event_first(picks, _backed)
            except Exception as _be:  # noqa: BLE001 - ordering is a preference, never a blocker
                logger.debug("swing: event-first ordering skipped: %s", _be)
        slots = lane.entry_slots(int(_cfg("SWING_MAX_POSITIONS", 3)), len(open_pos), int(_cfg("SWING_PICKS_PER_DAY", 3)))
        if slots <= 0 or not picks:
            return {**out, "status": "no_slots_or_picks", "detail": f"slots {slots}, picks {len(picks)}"}
        from market_feed.feed import get_quotes
        from portfolio.portfolio import get_account
        from execution import dhan_client
        from market_context.sector_signal import NSE_SECTOR_MAP
        import manual_engine
        ticks = await get_quotes([p["symbol"] for p in picks], priority=True)
        acct = get_account(db, mode)
        equity = float(acct.current_equity or 0.0)
        value = sum(float(p.avg_entry_price or 0) * int(p.qty_open or 0) for p in open_pos)
        sectors = [NSE_SECTOR_MAP.get(p.symbol) for p in open_pos if NSE_SECTOR_MAP.get(p.symbol)]
        slip = float(_cfg("SWING_SLIPPAGE_PCT", 0.10))
        for pk in picks:
            if out["placed"] >= slots:
                break
            sym = pk["symbol"]
            tick = ticks.get(sym)

            def skip(reason: str, _pk=pk) -> None:
                out["skipped"].append(f"{_pk['symbol']}: {reason}")
                repo.add_trade(kind="LIVE", mode=mode, symbol=_pk["symbol"], status="SKIPPED", signal_day=_pk["signal_day"], rank=_pk.get("rank"),
                               pct_rank=_pk.get("pct_rank"), signal_close=_pk.get("signal_close"), atr_pct=_pk.get("atr_pct"), stop_pct=_pk.get("stop_pct"),
                               skip_reason=reason[:160])
            if sym in held:
                skip("already held")
                continue
            if tick is None or not tick.price:
                skip("no live price")
                continue
            if mode == "REAL" and bool(_cfg("ENTRY_TRADABLE_GATE_ENABLED", True)):      # E264: Dhan cannot trade this symbol (series BE / not listed)
                _nt = dhan_client.untradable_reason(db, sym)
                if _nt:
                    skip(_nt)
                    continue
            sec = NSE_SECTOR_MAP.get(sym)
            if sec and sectors.count(sec) >= int(_cfg("SWING_MAX_PER_SECTOR", 1)):
                skip(f"sector {sec} already held")
                continue
            if lane.gap_too_big(getattr(tick, "open_price", None) or tick.price, pk.get("signal_close"), float(_cfg("SWING_MAX_GAP_UP_PCT", 5.0))):
                skip("gapped up too far over the signal close")
                continue
            if bool(_cfg("SWING_EVENT_VETO_ENABLED", True)):      # E274 round 47: a fresh negative typed event on the candidate (market hours or the after-hours veto row) refuses the buy
                try:
                    from watchlist_engine.intraday_news import event_veto_reason
                    _evr = event_veto_reason(db, mode, sym)      # cached snapshot + one indexed query: run inline, the session is not thread-safe
                except Exception:  # noqa: BLE001
                    _evr = None
                if _evr:
                    skip(f"EVENT_VETO: {_evr}")
                    continue
            veto = await asyncio.to_thread(engine.entry_vetoes, sym, td)
            if veto:
                skip("; ".join(veto))
                continue
            if bool(_cfg("SWING_OPEN_CONFIRM_ENABLED", True)) and str(_cfg("ORB_MODE", "live")).lower() != "off":      # E273 round 15: skip a pick whose first 5 minutes reversed
                oc = await asyncio.to_thread(_open_check, sym)
                if oc.get("ok") is False:
                    skip(f"OPEN_REVERSAL: {oc.get('detail') or oc.get('why')}")
                    continue
            limit = dhan_client.round_to_tick(float(tick.price) * (1.0 + slip / 100.0))
            sp = float(pk.get("stop_pct") or 3.0)
            stop = dhan_client.round_stop_to_tick(limit * (1.0 - sp / 100.0)) if hasattr(dhan_client, "round_stop_to_tick") else round(limit * (1 - sp / 100.0), 2)
            maxv = float(_cfg("SWING_MAX_TRADE_VALUE", 20000.0))
            qty = lane.entry_qty(limit, maxv, equity, float(_cfg("SWING_MAX_EXPOSURE_PCT", 40.0)), value)
            if qty < 1:
                skip("no room: size / exposure cap")
                continue
            req = SimpleNamespace(symbol=sym, side="BUY", qty=qty, order_type="LIMIT", product_type="CNC", limit_price=limit, stop_price=stop,
                                  target_price=None, position_id=None)
            res = await manual_engine.evaluate_manual_order(db, mode, gate_armed, req, confirm=True, admin="swing-lane", swing={"max_trade_value": maxv})
            if res.get("ok") and res.get("order_id"):
                approved = int(res.get("approved_qty") or qty)
                repo.add_trade(kind="LIVE", mode=mode, symbol=sym, status="OPEN", signal_day=pk["signal_day"], rank=pk.get("rank"), pct_rank=pk.get("pct_rank"),
                               signal_close=pk.get("signal_close"), atr_pct=pk.get("atr_pct"), stop_pct=sp, entry_day=today, entry_price=limit, stop_px=stop,
                               qty=approved, size_value=round(approved * limit, 2), order_id=int(res["order_id"]), position_id=None,
                               note=f"{res.get('status')}")
                out["placed"] += 1
                value += approved * limit
                held.add(sym)
                if sec:
                    sectors.append(sec)
                await _notify(f"🌱 *Swing entry — {mode}* {sym}\n{approved} @ ₹{limit:.2f} (CNC) | stop ₹{stop:.2f} ({sp:.1f}%) | rank {pk.get('rank')} | "
                              f"max {hold_days(db)} sessions\n{res.get('status')}"
                              + (f"\nEvent: {_backed[sym].get('type')} (strength {float(_backed[sym].get('strength') or 0):.2f})" if isinstance(_backed.get(sym), dict) else ""))
            else:
                skip(f"{res.get('reason')}: {str(res.get('detail') or res.get('check_name') or '')[:100]}")
        out["status"] = "done"
        return out
    except Exception as e:  # noqa: BLE001
        logger.exception("swing entries failed for %s", mode)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {**out, "status": "error", "detail": f"{type(e).__name__}: {str(e)[:120]}"}


async def _notify(msg: str) -> None:
    try:
        from notifier import notify_async
        await notify_async(msg)
    except Exception:  # noqa: BLE001
        logger.debug("swing notify failed", exc_info=True)


# ───────────────────────────── 15:00 hold decision ─────────────────────────────

async def eod_holds(db: Any, repo: Any, mode: str, positions: List[Any]) -> Tuple[Set[int], Dict[int, str]]:
    """(ids of swing positions that skip today's square-off, id -> reason). Never raises; any failure keeps nothing (positions are sold)."""
    keep: Set[int] = set()
    reasons: Dict[int, str] = {}
    try:
        mine = [p for p in positions if is_swing(p)]
        if not mine:
            return keep, reasons
        import models
        from tz_utils import ist_today_str
        if lane.norm_mode(_cfg("SWING_MODE", "live")) != "live":
            for p in mine:
                db.add(models.TradePositionEvent(position_id=p.id, event_type="SWING_EXIT_DECISION", detail="SWING_MODE is not live: swing position is not carried"))
            db.commit()
            return keep, reasons
        from market_feed.feed import get_quotes
        from entry_engine import calendar_gate as cg
        scan = engine.latest_scan(repo)
        rows = scan["rows"] if scan else {}
        today = date.fromisoformat(ist_today_str())
        ticks = await get_quotes([p.symbol for p in mine], priority=True)
        ctx = None
        if bool(_cfg("CARRY_CONTEXT_GATE_ENABLED", True)):
            try:
                from entry_engine import carry_context as _cc, global_cues as _gc, vix_gate as _vg
                ctx = (await asyncio.to_thread(_cc.nifty_change_pct), await asyncio.to_thread(_vg.snapshot), await asyncio.to_thread(_gc.snapshot), _cc)
            except Exception:  # noqa: BLE001
                ctx = None
        ledger = {t["position_id"]: t for t in repo.trades(kind="LIVE", mode=mode) if t.get("position_id")}
        cap = cg.carry_max_positions(int(_cfg("SWING_MAX_POSITIONS", 3)))
        for p in sorted(mine, key=lambda x: x.opened_at or datetime.now(timezone.utc)):
            tick = ticks.get(p.symbol)
            opened = _day(p.opened_at) or ist_today_str()
            sessions = lane.count_sessions(date.fromisoformat(opened), today, cg._is_trading_day)
            vetoes: List[str] = []
            try:
                from entry_engine import event_risk
                v = await asyncio.to_thread(event_risk.reject_reason, p.symbol, carry=True)
                if v:
                    vetoes.append(str(v))
                b = lane.earnings_block(await asyncio.to_thread(event_risk.lookup, p.symbol), today, int(_cfg("SWING_EARNINGS_WINDOW_DAYS", 7)))
                if b:
                    vetoes.append(b)
                if ctx is not None:
                    r = await asyncio.to_thread(lambda: ctx[3].reason(p.symbol, ctx[0], ctx[1], global_snap=ctx[2]))
                    if r:
                        vetoes.append(str(r))
            except Exception:  # noqa: BLE001 - unknown is not a veto (same rule as the overnight selector)
                pass
            row = rows.get(p.symbol) or {}
            _ext = maybe_extend(db, p, getattr(tick, "price", None), sessions)
            ok, why = lane.hold_verdict(sessions=sessions, max_hold=hold_days(db) + _ext, ltp=getattr(tick, "price", None), stop=p.current_stop,
                                        sma10=row.get("sma10"), pct_rank=row.get("pct_rank"), keep_min_pct_rank=float(_cfg("SWING_KEEP_MIN_PCT_RANK", 50.0)),
                                        require_above_sma10=bool(_cfg("SWING_KEEP_ABOVE_SMA10", True)), vetoes=vetoes)
            if ok and len(keep) >= cap:
                ok, why = False, f"carry cap {cap} for today's calendar reached"
            if ok:
                keep.add(p.id)
                reasons[p.id] = "swing: " + why
            else:
                db.add(models.TradePositionEvent(position_id=p.id, event_type="SWING_EXIT_DECISION", detail=why[:300]))
                t = ledger.get(p.id)
                if t:
                    repo.update_trade(t["id"], note=("exit decision: " + why)[:300], sessions=sessions)
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.exception("swing eod holds failed for %s: nothing is carried", mode)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return set(), {}
    return keep, reasons


# ───────────────────────────── evening reconcile and ratchet ─────────────────────────────

def after_scan(db: Any, repo: Any, scan: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """Link LIVE ledger rows to their positions, close them with the position's result, ratchet open stops from today's real close. Never raises."""
    n = {"linked": 0, "closed": 0, "ratcheted": 0, "dead": 0}
    try:
        import models
        from entry_engine import calendar_gate as cg
        rows = scan["rows"] if scan else {}
        asof = scan["asof"] if scan else None
        for t in repo.trades(kind="LIVE", status="OPEN"):
            pos = db.get(models.TradePosition, t["position_id"]) if t.get("position_id") else None
            if pos is None:
                q = db.query(models.TradePosition).filter(models.TradePosition.mode == t["mode"], models.TradePosition.symbol == t["symbol"],
                                                          models.TradePosition.entry_decision_label == lane.LABEL).order_by(models.TradePosition.id.desc()).first()
                if q is not None and (_day(q.opened_at) or "") >= (t.get("entry_day") or ""):
                    pos = q
            if pos is None:
                od = db.get(models.TradeOrder, t["order_id"]) if t.get("order_id") else None
                if od is not None and od.status in ("EXPIRED", "REJECTED", "CANCELLED"):
                    repo.update_trade(t["id"], status="SKIPPED", skip_reason=f"order {od.status.lower()} unfilled")
                    n["dead"] += 1
                continue
            f: Dict[str, Any] = {}
            if not t.get("position_id"):
                f.update(position_id=pos.id, entry_price=round(float(pos.avg_entry_price), 4))
                n["linked"] += 1
            if pos.status == "CLOSED":
                qty = int(t.get("qty") or 0) or 1
                entry = float(pos.avg_entry_price)
                gross = float(pos.realized_pnl or 0.0) / (entry * qty) * 100.0
                net = (float(pos.net_realized_pnl) / (entry * qty) * 100.0) if pos.net_realized_pnl is not None else None
                sell = db.query(models.TradeOrder).filter(models.TradeOrder.mode == t["mode"], models.TradeOrder.symbol == t["symbol"],
                                                          models.TradeOrder.side == "SELL").order_by(models.TradeOrder.id.desc()).first()
                f.update(status="CLOSED", exit_day=_day(pos.closed_at), exit_reason=(getattr(sell, "exit_reason", None) or "closed")[:24],
                         gross_pct=round(gross, 4), net_pct=None if net is None else round(net, 4),
                         cost_pct=None if net is None else round(gross - net, 4), closed_at=(pos.closed_at.replace(tzinfo=None).isoformat() if pos.closed_at else None),
                         exit_price=round(entry * (1 + gross / 100.0), 4))
                n["closed"] += 1
            elif asof and (rows.get(t["symbol"]) or {}).get("last_bar_day") == asof:
                r = rows[t["symbol"]]
                new = lane.ratcheted_stop(pos.current_stop, r.get("last_close"), t.get("stop_pct"))
                if new is not None and (pos.current_stop is None or new > float(pos.current_stop)):
                    old = pos.current_stop
                    pos.current_stop = new
                    db.add(models.TradePositionEvent(position_id=pos.id, event_type="STOP_TRAILED",
                                                     detail=f"swing ratchet after the {asof} close: ₹{old} → ₹{new} (close ₹{r.get('last_close')}, stop {t.get('stop_pct')}%)"))
                    db.commit()
                    n["ratcheted"] += 1
                opened = _day(pos.opened_at) or asof
                f.update(stop_px=pos.current_stop, last_close=r.get("last_close"), last_day=asof,
                         sessions=lane.count_sessions(date.fromisoformat(opened), date.fromisoformat(asof), cg._is_trading_day))
            if f:
                repo.update_trade(t["id"], **f)
    except Exception as e:  # noqa: BLE001
        logger.exception("swing after_scan failed")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        n["error"] = f"{type(e).__name__}: {str(e)[:100]}"
    return n
