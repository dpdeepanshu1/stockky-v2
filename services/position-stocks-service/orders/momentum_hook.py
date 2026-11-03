"""orders/momentum_hook.py - momentum-health exit for OPEN trades (E275, 31 Oct 2026).

One pass per fast-reconcile cycle (main.py), right after the tape hook. The decision is the pure `momentum_health.decide` (5 signals: exhaustion, no new
best price in time, climax bar, wall / typical gain, lost VWAP or level); this file reads the live price, the trade's 1-minute bars since entry, the
order-flow verdict and the fade check, then acts:
  * TIGHTEN: Super Order stop / target legs through dhan_client.modify_super_order (stop read back with verify_stop_leg); a plain short only moves its
    software target (E114: a plain short's broker stop is fixed by design). A stop is never loosened, a target never raised.
  * BOOK: the whole position is closed through eod_squareoff.close_position_now(exit_reason="MOMENTUM_EXIT") - the same cancel-legs-then-SELL path the
    stagnation exit uses. With MOMENTUM_SCALEOUT on (default off, round 164) an eligible long Super Order trade is split instead (orders/scaleout.py: half sold,
    half kept under a new stop); any failure there falls back to this full close, and the record says "HALF" or "FULL".
  * MOMENTUM_EXIT_MODE live | off (default live since E276 round 66; the old log-only mode is gone). Live stamps flow_json["momentum"]. Per-position MOMENTUM_EXIT_MIN_INTERVAL_S.
Never raises; a trade carried overnight (CNC) is left alone.
"""
from __future__ import annotations

import json
import logging
import time as _time
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

import config
from execution import dhan_client
from models import ScalpPosition
from orders import exit_adaptive, flow_gate, momentum_health, setup_stamp, setup_stats, side as side_mod
from tz_utils import as_aware

logger = logging.getLogger("position-stocks-momentum-hook")

_last_run: dict = {}
_stats = {"passes": 0, "tighten": 0, "book": 0, "booked": 0, "errors": 0}


def mode() -> str:
    m = str(getattr(config, "MOMENTUM_EXIT_MODE", "live") or "live").strip().lower()
    return "off" if m == "off" else "live"


def stats() -> dict:
    return {**_stats, "mode": mode()}


def _throttled(key) -> bool:
    last = _last_run.get(key)
    return bool(last is not None and _time.monotonic() - last < float(getattr(config, "MOMENTUM_EXIT_MIN_INTERVAL_S", 20.0)))


def _mark(key) -> None:
    if len(_last_run) > 2000:
        _last_run.clear()
    _last_run[key] = _time.monotonic()


def _ltp(symbol: str) -> Optional[float]:
    try:
        from feed import ws_client
        buf = ws_client.get_tick_buffer(symbol)
        v = buf[-1][1] if buf else None
        return float(v) if v and v > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _trade_bars(symbol: str, opened_ts: float) -> list:
    """The symbol's 1-minute bars from the entry minute on (the bar the entry fell in included), [] when the store has none."""
    try:
        from feed import bar_store
        return [b for b in bar_store.get_bars(symbol) if float(b[0]) + 60.0 > opened_ts]
    except Exception:  # noqa: BLE001
        return []


def stamp_momentum(flow_json, record: dict):
    """Append `record` to flow_json["momentum"] (a list, newest last, at most 10). Record only; returns `flow_json` unchanged on any problem. Never raises."""
    try:
        d = json.loads(flow_json) if isinstance(flow_json, str) and flow_json else (dict(flow_json) if isinstance(flow_json, dict) else {})
        if not isinstance(d, dict):
            d = {}
        lst = d.get("momentum")
        lst = list(lst) if isinstance(lst, list) else []
        lst.append(record)
        d["momentum"] = lst[-10:]
        return json.dumps(d, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001
        return flow_json


def learned_numbers(db, pos, target_pct):
    """E275 round 37: this trade's setup cell supplies signal 2's minutes (its winners' median time-to-peak) and signal 4's typical gain, shrunk toward the fixed
    MOMENTUM_NO_NEW_BEST_MIN and the trade's own target (setup_stats.exit_numbers). Fixed numbers when MOMENTUM_LEARNED_ENABLED is off, the trade has no setup stamp or
    anything fails. {"ttp_min", "typical_gain_pct", "ttp_source", "gain_source"}; never raises."""
    fixed = float(getattr(config, "MOMENTUM_NO_NEW_BEST_MIN", 20.0))
    base = {"ttp_min": fixed, "typical_gain_pct": None, "ttp_source": "fixed", "gain_source": "none"}
    try:
        if not bool(getattr(config, "MOMENTUM_LEARNED_ENABLED", True)):
            return base
        st = setup_stamp.read(getattr(pos, "flow_json", None))
        if not st:
            return base
        from orders import slot_ladder
        cell = (slot_ladder.cells(db) or {}).get(setup_stats.cell_key(st))
        out = setup_stats.exit_numbers(cell, ttp_fallback_min=fixed, target_pct=target_pct)
        from orders import learned_limit
        out = learned_limit.limit_exit_numbers(setup_stats.cell_key(st), "momentum", out, fixed)      # E275 round 39: at most 25 % a week
        out["ttp_min"] = max(8.0, min(60.0, float(out["ttp_min"])))      # never a hair trigger, never longer than an hour
        if out.get("gain_source") == "fallback":
            out["typical_gain_pct"] = None                                   # nothing learned: signal 4 stays wall-only, as before
        return out
    except Exception:  # noqa: BLE001
        return base


def _tape_verdict(symbol: str, side: str) -> Optional[dict]:
    try:
        return flow_gate._fetch_exit_verdict(symbol, side)   # noqa: SLF001 - the same one uncached call the tape exit makes
    except Exception:  # noqa: BLE001
        return None


def _apply_tighten(db: Session, pos, d: dict, ltp: float, plain: bool, short: bool) -> bool:
    new_stop, new_target = d.get("new_stop"), d.get("new_target")
    s = -1 if short else 1
    if plain:
        new_stop = None                                    # E114: never touch a plain short's broker stop
    if new_stop is not None and pos.stop_price is not None and s * (float(new_stop) - float(pos.stop_price)) <= 0:
        new_stop = None                                    # never loosen
    if new_target is not None and pos.target_price is not None and s * (float(new_target) - float(pos.target_price)) >= 0:
        new_target = None                                  # never raise a long's / lower a short's target
    if new_stop is None and new_target is None:
        return False
    old_stop, old_target = pos.stop_price, pos.target_price
    changed = False
    if new_stop is not None:
        try:
            dhan_client.modify_super_order(db, order_id=pos.dhan_super_order_id, order_leg="STOP_LOSS_LEG", stop_loss_price=new_stop)
            if dhan_client.verify_stop_leg(db, order_id=pos.dhan_super_order_id, expected_price=new_stop) is False:
                logger.warning("MOMENTUM_EXIT: %s (id=%d) stop modify to %.2f NOT confirmed by Dhan - stored stop left at %s", pos.symbol, pos.id, new_stop, pos.stop_price)
            else:
                pos.stop_price = new_stop
                pos.stop_moved_to_breakeven = True         # so breakeven.py can never lower it again
                changed = True
        except Exception as e:  # noqa: BLE001
            logger.warning("MOMENTUM_EXIT: %s (id=%d) stop modify to %.2f rejected (%s) - leg unchanged", pos.symbol, pos.id, new_stop, e)
    if new_target is not None:
        if plain:
            pos.target_price = new_target
            changed = True
        else:
            try:
                dhan_client.modify_super_order(db, order_id=pos.dhan_super_order_id, order_leg="TARGET_LEG", target_price=new_target)
                pos.target_price = new_target
                changed = True
            except Exception as e:  # noqa: BLE001
                logger.warning("MOMENTUM_EXIT: %s (id=%d) target modify to %.2f rejected (%s) - leg unchanged", pos.symbol, pos.id, new_target, e)
    if changed:
        pos.flow_json = stamp_momentum(getattr(pos, "flow_json", None), {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "action": "TIGHTEN", "ltp": round(float(ltp), 2), "old_stop": old_stop,
            "new_stop": pos.stop_price, "old_target": old_target, "new_target": pos.target_price, "signals": d.get("signals"), "reason": str(d.get("reason") or "")[:160], "learned": d.get("learned")})
        db.commit()
        logger.info("MOMENTUM_EXIT: %s (id=%d) %s -> stop %s target %s (ltp %.2f)", pos.symbol, pos.id, d.get("reason"), pos.stop_price, pos.target_price, ltp)
    return changed


def _scaleout_ok(pos) -> bool:
    try:
        from orders import scaleout
        return scaleout.eligible(pos)[0]
    except Exception:  # noqa: BLE001
        return False


def _apply_half(db: Session, pos, d: dict, ltp: float) -> Optional[bool]:
    """MOMENTUM_SCALEOUT: split the position. True = split (or the rest closed), False = nothing to do this pass, None = close in full the old way. Never raises."""
    try:
        from orders import scaleout
        r = scaleout.run_half_sell(db, pos, ltp=ltp, decision=d)
    except Exception as e:  # noqa: BLE001
        logger.warning("MOMENTUM_EXIT: %s (id=%d) scale-out failed (%s) - closing in full", pos.symbol, pos.id, e)
        return None
    if r.get("fallback") == "FULL":
        logger.warning("MOMENTUM_EXIT: %s (id=%d) scale-out not done (%s) - closing in full", pos.symbol, pos.id, r.get("reason"))
        return None
    return bool(r.get("ok")) or r.get("fallback") == "HALF_THEN_FULL"


def _apply_book(db: Session, pos, d: dict, ltp: float) -> bool:
    from orders import eod_squareoff
    pos.flow_json = stamp_momentum(getattr(pos, "flow_json", None), {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "action": "BOOK", "mode": d.get("book_mode"), "ltp": round(float(ltp), 2),
        "signals": d.get("signals"), "reason": str(d.get("reason") or "")[:160], "learned": d.get("learned")})
    try:
        eod_squareoff._stamp_exit_rule(pos, "momentum")    # noqa: SLF001 - same record as the stagnation exit's exit_rule
        eod_squareoff.close_position_now(db, pos, exit_reason="MOMENTUM_EXIT")
        logger.warning("MOMENTUM_EXIT: %s (id=%d) booked - %s (ltp %.2f)", pos.symbol, pos.id, d.get("reason"), ltp)
        return True
    except eod_squareoff.ManualCloseRejected as e:
        db.rollback()
        logger.info("MOMENTUM_EXIT: %s (id=%d) book skipped - %s", pos.symbol, pos.id, e)
    return False


def run_momentum_exit(db: Session) -> int:
    """Returns the number of positions whose stop / target moved or that were booked this pass."""
    md = mode()
    if md == "off":
        return 0
    _stats["passes"] += 1
    acted = 0
    try:
        positions = db.query(ScalpPosition).filter(ScalpPosition.status == "OPEN").all()
    except Exception as e:  # noqa: BLE001
        logger.warning("momentum-exit: could not load positions: %s", e)
        return 0
    now = datetime.now(timezone.utc)
    for pos in positions:
        try:
            if getattr(pos, "overnight_converted_to_cnc", False):
                continue
            plain = bool(getattr(pos, "plain_stop_order_id", None)) and not getattr(pos, "dhan_super_order_id", None)
            if not plain and not getattr(pos, "dhan_super_order_id", None):
                continue                                   # nothing we can move or close safely
            key = ("ps-mom", pos.id)
            if _throttled(key):
                continue
            ltp = _ltp(pos.symbol)
            if ltp is None or not pos.entry_price:
                continue
            opened = as_aware(pos.opened_at)
            opened_ts = opened.timestamp()
            bars = _trade_bars(pos.symbol, opened_ts)
            if len(bars) < 3:
                continue                                   # no 1-minute history for this trade: no opinion
            short = side_mod.is_short(pos)
            side = "SHORT" if short else "LONG"
            _mark(key)
            tgt_pct = abs(float(pos.target_price) / float(pos.entry_price) - 1.0) * 100.0 if pos.target_price else None
            learned = learned_numbers(db, pos, tgt_pct)
            d = momentum_health.decide(
                side=side, entry=pos.entry_price, stop=pos.stop_price, target=pos.target_price, ltp=ltp, qty=pos.quantity, bars=bars,
                tick=dhan_client.tick_size_for_price(ltp), age_min=(now - opened).total_seconds() / 60.0, tape=_tape_verdict(pos.symbol, side),
                fading=exit_adaptive.is_fading(pos.symbol, ltp, short=short, entry=pos.entry_price),
                no_new_best_min=learned["ttp_min"], typical_gain_pct=learned["typical_gain_pct"], scaleout=_scaleout_ok(pos),
                book_n=int(getattr(config, "MOMENTUM_BOOK_N", 3)), min_book_profit_pct=float(getattr(config, "MOMENTUM_BOOK_MIN_PROFIT_PCT", 0.2)))
            d["learned"] = {k: learned.get(k) for k in ("ttp_min", "typical_gain_pct", "ttp_source", "gain_source")}
            act = d.get("action")
            if act not in ("TIGHTEN", "BOOK"):
                continue
            _stats["tighten" if act == "TIGHTEN" else "book"] += 1
            if act == "TIGHTEN":
                if _apply_tighten(db, pos, d, ltp, plain, short):
                    acted += 1
            elif d.get("book_mode") == "HALF" and _scaleout_ok(pos):
                _h = _apply_half(db, pos, d, ltp)
                if _h:
                    _stats["booked"] += 1
                    acted += 1
                elif _h is None and _apply_book(db, pos, d, ltp):
                    _stats["booked"] += 1
                    acted += 1
            elif _apply_book(db, pos, d, ltp):
                _stats["booked"] += 1
                acted += 1
        except Exception as e:  # noqa: BLE001
            _stats["errors"] += 1
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                logger.debug("momentum_hook: ignored error (fail-open)", exc_info=True)
            logger.warning("momentum-exit: %s failed: %s", getattr(pos, "symbol", "?"), e)
    return acted
