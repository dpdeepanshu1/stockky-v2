"""orders/setup_short.py - E274 round 40: S4_CATALYST_DOWN, the short on a negative typed event (Position Stocks).

setup_engine already knows S4 (negative event, price below the pre-open price, a COMPLETED 1-minute bar closed below the opening-range low, 2 of 3 confirmations: tape
selling, below VWAP, sector in the bottom third; refused when the move is already priced in). Nothing fed it for a short. This module builds the short facts and asks the engine:

  facts_short   the `facts` dict for side SHORT from what the short plan holds (quality event polarity, the SHORT opening-range record from orb_shadow, the cached quote, the
                pre-open gap, the tape verdict, the sector third and the priced-in ratio from setup_inputs). A fact that is missing is simply not true.
  decide        reads the live sources (each in its own try) and returns the engine's answer. {"id": None, ...} when S4 does not fire, is switched off, or anything fails.

A match is stamped on the short as S4_CATALYST_DOWN and sized by the engine (quarter size in the 09:20 slot, half later, promoted / demoted by the slot ladder); a short that matches
nothing keeps the legacy stamp and its normal size, exactly as before: there is no "no setup, no trade" for shorts yet.
E274 round 44: S2 (a weak stock of the day breaking the opening-range low or holding a rally-back) and S3 (exhaustion after an up-spike) may match a short too, from
setup_inputs.gather_short. They have their OWN stats cells ("S2_RS_BREAKOUT_SHORT|slot"), never the long ones. Switches: SETUP_ENGINE_ENABLED, SETUP_S4_ENABLED,
SETUP_S2_S3_SHORT_ENABLED (with SETUP_S2_ENABLED / SETUP_S3_ENABLED). Never raises.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

import config
from orders import setup_engine, setup_inputs, setup_live

logger = logging.getLogger("position-stocks-setup-short")


def enabled() -> bool:
    return bool(getattr(config, "SETUP_ENGINE_ENABLED", True) and (getattr(config, "SETUP_S4_ENABLED", True) or other_setups()))


def other_setups() -> set:
    """E274 round 44: the extra setups a SHORT may match (S2 weakness break, S3 exhaustion after an up-spike), each behind its own long switch and the short switch."""
    if not (getattr(config, "SETUP_ENGINE_ENABLED", True) and getattr(config, "SETUP_S2_S3_SHORT_ENABLED", True)):
        return set()
    return (({setup_engine.S2} if getattr(config, "SETUP_S2_ENABLED", True) else set()) | ({setup_engine.S3} if getattr(config, "SETUP_S3_ENABLED", True) else set()))


def allowed_set() -> set:
    return (({setup_engine.S4} if getattr(config, "SETUP_S4_ENABLED", True) else set()) | other_setups())


def board_bearish(row: Any) -> bool:
    """True when the Market Board row ranks the symbol bearish with at least SHORT_MIN_BOARD_CONVICTION. A missing / unreadable row is False (never raises)."""
    try:
        if not isinstance(row, dict) or str(row.get("side") or "") != "bear":
            return False
        conv = setup_live._num(row.get("conviction"))
        need = float(getattr(config, "SHORT_MIN_BOARD_CONVICTION", 50.0))
        return conv is not None and conv >= need
    except Exception:  # noqa: BLE001
        return False


def facts_short(*, symbol: str, price: Optional[float], now: Optional[datetime], event_polarity: Optional[int], orb: Any, quote: Optional[dict],
                gap_pct: Optional[float], tape: Any = None, inputs: Optional[Dict[str, Any]] = None, penalties: Optional[Dict[str, float]] = None,
                board_row: Any = None, allowed: Optional[set] = None) -> Dict[str, Any]:
    """The setup_engine facts of one short. `orb` is the SHORT opening-range record (orb_shadow.evaluate(..., side="SHORT")): range_low and the closed-bar trigger."""
    f: Dict[str, Any] = {"side": "SHORT", "service": "PS", "now": now, "allowed": set(allowed) if allowed else {setup_engine.S4}, "penalties": dict(penalties or {})}
    try:
        f["price"] = setup_live._num(price)
        f["event_polarity"] = int(event_polarity) if event_polarity in (1, -1) else 0
        o = orb if isinstance(orb, dict) else {}
        f["or_low"] = setup_live._num(o.get("range_low"))
        if o.get("close_trigger") is True:
            f["bar_close"] = setup_live._num(o.get("trigger_close"))
            f["bar_closed_after_range"] = True                      # bar_fields only counts bars that start after the range end
        q = quote if isinstance(quote, dict) else {}
        pc = setup_live._num(q.get("previous_close") if q.get("previous_close") is not None else q.get("prev_close"))
        g = setup_live._num(gap_pct)
        if pc and pc > 0 and g is not None:
            f["preopen_price"] = pc * (1.0 + g / 100.0)
        vw = setup_live._num(q.get("avg_price"))
        if vw and vw > 0 and f["price"]:
            f["above_vwap"] = f["price"] >= vw
        f["tape"] = setup_live.tape_sign(tape)                       # -1 = selling = the short's confirmation
        if getattr(config, "SETUP_S4_BOARD_CONFIRM_ENABLED", True):
            f["board_bear"] = board_bearish(board_row)                  # E274 round 42: the 4th S4 confirmation
        if isinstance(inputs, dict):
            f.update({k: v for k, v in inputs.items() if k in setup_live.INPUT_KEYS})          # E274 round 44: S2 / S3 short facts ride in the same dict
    except Exception:  # noqa: BLE001
        pass
    return f


def gather(*, symbol: str, price: Any, quote: Any, board_payload: Any = None, sector: Optional[str] = None, prev_atr_pct: Any = None) -> Dict[str, Any]:
    """S4's two extras: the sector third (bottom is the confirmation) and how far the fall is already priced in (price move below the previous close / expected move)."""
    out: Dict[str, Any] = {}
    q = quote if isinstance(quote, dict) else {}
    pc = q.get("previous_close") if q.get("previous_close") is not None else q.get("prev_close")
    try:
        payload = board_payload
        if payload is None:
            from screening import board
            payload = board.build(None, None, [])
        if sector is None:
            from feed import sector_map
            sector = sector_map.sector_of(symbol)
        st = setup_inputs.sector_third(payload, sector)
        if not st:                                               # E272: pre-open sector ranking from the premarket plan while the board cannot rank yet
            from screening import premarket_plan
            st = premarket_plan.sector_third(symbol, sector=sector)
        if st:
            out["sector_third"] = st
    except Exception:  # noqa: BLE001
        pass
    try:
        atr = prev_atr_pct
        if atr is None:
            from screening import prev_day
            pdv = prev_day.peek(symbol)
            atr = getattr(pdv, "atr_pct", None) if pdv is not None else None
        mult = float(getattr(config, "SETUP_PRICED_IN_ATR_MULT", setup_inputs.PRICED_IN_ATR_MULT))
        pi = setup_inputs.priced_in(price, pc, atr, -1, mult)
        if pi is not None:
            out["event_priced_in"] = pi
    except Exception:  # noqa: BLE001
        pass
    return out


def decide(*, symbol: str, price: float, now: Optional[datetime], quality: Any, tape_json: Any = None, penalties: Optional[Dict[str, float]] = None,
           quote: Any = None, orb: Any = None, gap_pct: Any = None, inputs: Optional[Dict[str, Any]] = None, db: Any = None, board_row: Any = None) -> Dict[str, Any]:
    """setup_engine.classify for one short. The keyword arguments that carry a value skip the live read (that is how the tests run it). A match carries the engine's
    size_mult (slot ladder demotion applied) and its stamp; anything else is {"id": None, ...}. Never raises."""
    none: Dict[str, Any] = {"id": None, "skip": "NO_SETUP"}
    try:
        if not enabled():
            return {"id": None, "skip": "OFF"}
        pol = getattr(quality, "event_polarity", None) if quality is not None else None
        allowed = allowed_set()
        extra = allowed - {setup_engine.S4}
        if pol != -1 and not extra:
            return none                                          # S4's trigger is a negative typed event: nothing to read without one (and no S2 / S3 short is switched on)
        if pol != -1:
            allowed = extra                                      # without a negative event only S2 / S3 can fire
        if quote is None:
            from orders import depth_gate
            quote = depth_gate._fresh_quote(symbol)
        if orb is None:
            from orders import orb_shadow
            orb = orb_shadow.evaluate(symbol, quote, price, now, "SHORT")
        if gap_pct is None:
            from screening import preopen
            gap_pct = preopen.gap_of(symbol)
        if inputs is None:
            inputs = gather(symbol=symbol, price=price, quote=quote)
        inputs = dict(inputs or {})
        tape = None
        try:
            tj = tape_json
            if isinstance(tj, str):
                import json
                tj = json.loads(tj)
            tape = tj.get("tape") if isinstance(tj, dict) else None
        except Exception:  # noqa: BLE001
            tape = None
        if extra and not any(k in inputs for k in ("z_move", "rs_pct", "or_break")):
            try:                                                 # E274 round 44: the S2 / S3 short facts (one read of the bars, each source in its own try)
                import time as _t
                inputs.update(setup_inputs.gather_short(symbol=symbol, price=price, now_ts=(now.timestamp() if isinstance(now, datetime) else _t.time()), orb=orb, quote=quote,
                                                        gap_pct=gap_pct, tape=tape, want_s2=setup_engine.S2 in extra, want_s3=setup_engine.S3 in extra))
            except Exception:  # noqa: BLE001
                pass
        ladder = None
        try:
            from orders import slot_ladder
            ladder = slot_ladder.get(db) if db is not None else None
            if ladder and ladder.get("promoted"):
                # the engine looks a cell up as "<id>|<slot>": hand it the SHORT cells under the plain ids and none of the long S2 / S3 ones
                inputs["promoted"] = {k.replace("_SHORT|", "|") for k in ladder["promoted"] if k.startswith(setup_engine.S4) or "_SHORT|" in k}
        except Exception:  # noqa: BLE001
            ladder = None
        res = setup_engine.classify(facts_short(symbol=symbol, price=price, now=now, event_polarity=pol, orb=orb, quote=quote, gap_pct=gap_pct, tape=tape,
                                                inputs=inputs, penalties=penalties, board_row=board_row, allowed=allowed))
        if res.get("id") and res.get("size_mult") is not None and ladder:
            from orders import slot_ladder
            df = slot_ladder.demote_factor(res["id"], res.get("slot"), ladder, side="SHORT")
            if df < 1.0:
                res["size_mult"] = round(max(setup_engine.SIZE_FLOOR, float(res["size_mult"]) * df), 4)
        return res
    except Exception as e:  # noqa: BLE001
        logger.debug("short setup decide failed for %s: %s", symbol, e)
        return {"id": None, "skip": "NO_SETUP", "why": "error"}


def stamp(flow_json: Any, res: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> Any:
    """flow_json text with the short's setup stamp: the matched S4, or the legacy shortlist stamp (side SHORT) when nothing matched. SETUP_STAMP_ENABLED off = unchanged."""
    try:
        if not getattr(config, "SETUP_STAMP_ENABLED", True):
            return flow_json
        from orders import setup_stamp
        if isinstance(res, dict) and res.get("id"):
            st = setup_stamp.build(setup_id=res["id"], trigger=res.get("trigger", ""), confirmations=res.get("confirmations"),
                                   penalties=(res.get("stamp") or {}).get("penalties"), size_mult=res.get("size_mult"), now=now, side="SHORT")
        else:
            st = setup_stamp.build(trigger="shortlist", now=now, side="SHORT")
        return setup_stamp.attach(flow_json, st)
    except Exception:  # noqa: BLE001
        return flow_json
