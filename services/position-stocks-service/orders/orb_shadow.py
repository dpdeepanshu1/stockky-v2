"""orders/orb_shadow.py - opening-range breakout for position-stocks (group 479; LIVE input since E273 round 8).

Same verdict shape as real-trade-service entry_engine/orb_shadow.py (group 470; that copy is still the old record-only one), computed from the cached market-data /quote row (day high / low, avg price
= VWAP, spread) at the moment a position is opened. Stored in scalp_positions.flow_json["orb"]; setup_engine reads range_high and the closed-bar trigger as the
S1 / S2 trigger (orders/setup_live.py, orders/setup_inputs.py). This module itself orders, blocks and resizes nothing: it measures.
RANGE (E273 round 8): the 09:15 to ORB_RANGE_END_IST (09:20) 1-minute bars from feed/bar_store give the exact range ("range_src": "bars"; at least ORB_BAR_RANGE_MIN_BARS
bars, ORB_BAR_RANGE_ENABLED). Fallback: the first quote seen between ORB_RANGE_END_IST and ORB_CAPTURE_UNTIL_IST (09:26) fixes range_high / range_low from the quote's
day high / low ("range_src": "quote"). Before E273 round 8 only that window existed, so a symbol first seen after 09:26 had no range (reason "no_range") and S1 / S2 could not fire;
now it has one whenever its range bars were recorded. record_quote() is still called for every entry candidate.
VERDICT: price above range_high, above VWAP, spread <= ORB_MAX_SPREAD_PCT, range width inside [ORB_MIN_RANGE_PCT, ORB_MAX_RANGE_PCT], time <= ORB_LATEST_ENTRY_IST.
limit = range_high x (1 + ORB_LIMIT_BUFFER_PCT %), stop = range_low.
E119: SHORT mirror (side="SHORT", used for a live short only): price BELOW range_low and below-not-above VWAP count, limit = range_low x (1 - buffer), stop = range_high, and the close-based trigger is the first completed bar
that closes BELOW range_low. A long record has no "side" key; a short record carries "side": "SHORT".
E104: bar_fields() adds a close-based trigger and the trigger bar's volume against the opening-range bars' average. Every function never raises. ORB_MODE=off stops all recording (then S1 / S2 have no range).
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import config
from tz_utils import ist_now, ist_time_at_or_after, parse_hhmm

logger = logging.getLogger("position-stocks-orb-shadow")

_ranges: Dict[Tuple[str, str], Dict[str, float]] = {}
_bar_ranges: Dict[Tuple[str, str], Dict[str, float]] = {}   # E273 round 8: ranges built from the recorded 1-minute bars (final once the range end has passed)
_stats: Dict[str, int] = {"recorded": 0, "would_enter": 0, "no_range": 0, "ranges": 0, "bar_ranges": 0}


def _f(v) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _t(name: str, dh: int, dm: int):
    return parse_hhmm(str(getattr(config, name, "") or ""), dh, dm)


def _off() -> bool:
    """ORB_MODE is live | off; every other value (including the old shadow) reads as live."""
    return str(getattr(config, "ORB_MODE", "live") or "live").strip().lower() == "off"


def record_quote(symbol: str, quote: Any, now: Optional[datetime] = None) -> None:
    """Fix the opening range from this quote when we are inside the capture window. Never raises."""
    try:
        if _off() or not isinstance(quote, dict):
            return
        n = ist_now(now)
        if n.weekday() >= 5:
            return
        in_capture = (ist_time_at_or_after(_t("ORB_RANGE_END_IST", 9, 20), now)
                      and not ist_time_at_or_after(_t("ORB_CAPTURE_UNTIL_IST", 9, 26), now))
        if not in_capture:
            return
        sym = str(symbol or "").strip().upper()
        key = (sym, n.date().isoformat())
        hi, lo = _f(quote.get("day_high")), _f(quote.get("day_low"))
        if key not in _ranges and hi and lo and hi > lo > 0:
            _ranges[key] = {"high": hi, "low": lo}
            _stats["ranges"] += 1
            if len(_ranges) > 3000:
                day = n.date().isoformat()
                for k in [k for k in _ranges if k[1] != day]:
                    _ranges.pop(k, None)
    except Exception as e:  # noqa: BLE001
        logger.debug("orb record_quote failed: %s", e)


def bar_range(symbol: str, now: Optional[datetime] = None, bars: Optional[Any] = None) -> Optional[Dict[str, float]]:
    """E273 round 8: the opening range from today's recorded 1-minute bars: highest high and lowest low of the bars that START in [09:15, ORB_RANGE_END_IST).
    {"high", "low", "n"} once the range end has passed and at least ORB_BAR_RANGE_MIN_BARS usable bars exist; None otherwise (before the range end, switch off, too few
    bars, unusable rows, high <= low). A found range is cached for the day: its bars are all complete after the range end. Never raises."""
    try:
        if not bool(getattr(config, "ORB_BAR_RANGE_ENABLED", True)):
            return None
        n = ist_now(now)
        sym = str(symbol or "").strip().upper()
        key = (sym, n.date().isoformat())
        hit = _bar_ranges.get(key)
        if hit is not None:
            return hit
        re_t = _t("ORB_RANGE_END_IST", 9, 20)
        end_ts = n.replace(hour=re_t.hour, minute=re_t.minute, second=0, microsecond=0).timestamp()
        if n.timestamp() < end_ts:
            return None
        if bars is None:
            from feed import bar_store
            bars = bar_store.get_bars(sym)
        if not bars:
            return None
        open_ts = n.replace(hour=9, minute=15, second=0, microsecond=0).timestamp()
        hi = lo = None
        used = 0
        for b in bars:                                                           # one unreadable row must not hide the rest
            try:
                t, h, l = _f(b[0]), _f(b[2]), _f(b[3])
            except (TypeError, IndexError, KeyError):
                continue
            if t is None or h is None or l is None or h <= 0 or l <= 0 or h < l or t < open_ts or t >= end_ts:
                continue
            hi = h if hi is None else max(hi, h)
            lo = l if lo is None else min(lo, l)
            used += 1
        if used < max(1, int(getattr(config, "ORB_BAR_RANGE_MIN_BARS", 3) or 3)) or hi is None or lo is None or not hi > lo:
            return None
        rng = {"high": hi, "low": lo, "n": float(used)}
        _bar_ranges[key] = rng
        _stats["bar_ranges"] += 1
        if len(_bar_ranges) > 3000:
            day = n.date().isoformat()
            for k in [k for k in _bar_ranges if k[1] != day]:
                _bar_ranges.pop(k, None)
        return rng
    except Exception as e:  # noqa: BLE001
        logger.debug("orb bar_range failed: %s", e)
        return None


def bar_fields(symbol: str, rh: float, now: Optional[datetime] = None, bars: Optional[Any] = None, side: str = "LONG") -> Dict[str, Any]:
    """E104 (P2 item 14): two extra record-only fields from today's 1-minute bars (feed/bar_store), next to the tick-based verdict.
    CLOSE-BASED TRIGGER: the first COMPLETED bar after ORB_RANGE_END_IST whose close is above range_high ("close_trigger"; a wick through the high
    that closes back inside is not a trigger). TRIGGER-BAR VOLUME: that bar's volume against the average volume of the bars inside the opening range
    (09:15 up to ORB_RANGE_END_IST): "volume_ratio" = trigger volume / range average. Nothing here changes would_enter or any reason.
    E119: side="SHORT" mirrors it: `rh` then carries the range LOW and the trigger is the first completed bar that closes BELOW it.
    {} when there are no bars; volume fields only when a trigger bar and at least one range bar exist. Never raises."""
    try:
        if bars is None:
            from feed import bar_store
            bars = bar_store.get_bars(str(symbol or "").strip().upper())
        if not bars or rh is None:
            return {}
        n = ist_now(now)
        now_ts = n.timestamp()
        re_t = _t("ORB_RANGE_END_IST", 9, 20)
        open_ts = n.replace(hour=9, minute=15, second=0, microsecond=0).timestamp()
        end_ts = n.replace(hour=re_t.hour, minute=re_t.minute, second=0, microsecond=0).timestamp()
        range_vols, trigger = [], None
        short = str(side or "").upper() == "SHORT"
        rows = []
        for b in bars:                                                           # one unreadable row must not hide the rest
            try:
                t, c, v = _f(b[0]), _f(b[4]), _f(b[5])
            except (TypeError, IndexError, KeyError):
                continue
            if t is not None and c is not None:
                rows.append((t, c, v))
        for t, c, v in sorted(rows, key=lambda r: r[0]):
            if t < open_ts or t + 60 > now_ts:                                   # before the open, or the bar is still forming
                continue
            if t < end_ts:
                if v is not None and v >= 0:
                    range_vols.append(v)
            elif trigger is None and ((c < rh) if short else (c > rh)):
                trigger = (t, c, v)
        out: Dict[str, Any] = {"close_trigger": trigger is not None, "n_range_bars": len(range_vols)}
        if trigger is not None:
            t, c, v = trigger
            out.update(trigger_bar_at=datetime.fromtimestamp(t, n.tzinfo).strftime("%H:%M"), trigger_close=round(c, 2))
            avg = sum(range_vols) / len(range_vols) if range_vols else 0.0
            if v is not None and avg > 0:
                out.update(trigger_volume=int(v), range_avg_volume=int(round(avg)), volume_ratio=round(v / avg, 2))
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("orb bar_fields failed: %s", e)
        return {}


def evaluate(symbol: str, quote: Any, ltp: Optional[float] = None, now: Optional[datetime] = None, side: str = "LONG") -> Optional[Dict[str, Any]]:
    """The ORB verdict dict, or None when off / after ORB_LATEST_ENTRY_IST / before the open / unusable. Pure apart from the range store."""
    try:
        if _off():
            return None
        n = ist_now(now)
        if n.weekday() >= 5 or n.time() < parse_hhmm("09:15", 9, 15):
            return None
        q = quote if isinstance(quote, dict) else {}
        record_quote(symbol, q, now)
        if n.time() > _t("ORB_LATEST_ENTRY_IST", 10, 30):
            return None
        key = (str(symbol or "").strip().upper(), n.date().isoformat())
        rng = bar_range(symbol, now)                                   # E273 round 8: the exact 09:15-09:20 range from the recorded bars first ...
        src = "bars" if rng is not None else "quote"
        if rng is None:
            rng = _ranges.get(key)                                     # ... the quote capture window (09:20-09:26) as the fallback
        px = _f(ltp) or _f(q.get("price"))
        out: Dict[str, Any] = {"v": 1, "mode": "live", "at": n.strftime("%H:%M:%S")}
        if rng is None or px is None:
            out.update(would_enter=False, reason="no_range" if rng is None else "no_price")
            return out
        rh, rl = rng["high"], rng["low"]
        width = (rh - rl) / rl * 100.0
        vwap, spread = _f(q.get("avg_price")), _f(q.get("spread_pct"))
        short = str(side or "").upper() == "SHORT"
        reasons = []
        if short:
            if px >= rl:
                reasons.append("above_range_low")
            if vwap is not None and vwap > 0 and px >= vwap:
                reasons.append("above_vwap")
        else:
            if px <= rh:
                reasons.append("below_range_high")
            if vwap is not None and vwap > 0 and px <= vwap:
                reasons.append("below_vwap")
        max_sp = float(getattr(config, "ORB_MAX_SPREAD_PCT", 0.0) or 0.0)
        if max_sp > 0 and spread is not None and spread > max_sp:
            reasons.append("wide_spread")
        if width < float(getattr(config, "ORB_MIN_RANGE_PCT", 0.0) or 0.0):
            reasons.append("range_too_narrow")
        max_w = float(getattr(config, "ORB_MAX_RANGE_PCT", 0.0) or 0.0)
        if max_w > 0 and width > max_w:
            reasons.append("range_too_wide")
        buf = float(getattr(config, "ORB_LIMIT_BUFFER_PCT", 0.0) or 0.0)
        out.update(range_high=round(rh, 2), range_low=round(rl, 2), range_pct=round(width, 3), range_src=src, would_enter=not reasons, reasons=reasons)
        if short:
            out.update(side="SHORT", limit_price=round(rl * (1 - buf / 100.0), 2), stop_price=round(rh, 2),
                       stop_pct=round((rh - px) / px * 100.0, 3) if rh > px else None)
            out.update(bar_fields(symbol, rl, now, side="SHORT"))    # E119: close BELOW the range low + trigger-bar volume
        else:
            out.update(limit_price=round(rh * (1 + buf / 100.0), 2), stop_price=round(rl, 2),
                       stop_pct=round((px - rl) / px * 100.0, 3) if px > rl else None)
            out.update(bar_fields(symbol, rh, now))      # E104: close-based trigger + trigger-bar volume 
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("orb evaluate failed: %s", e)
        return None


def attach(flow_json: Optional[str], symbol: str, quote: Any, ltp: Optional[float] = None, now: Optional[datetime] = None, side: str = "LONG") -> Optional[str]:
    """flow_json text with the ORB verdict merged under "orb"; the input unchanged when nothing was recorded. Never raises."""
    try:
        import json
        v = evaluate(symbol, quote, ltp, now, side)
        if v is None:
            return flow_json
        try:
            base = json.loads(flow_json) if flow_json else {}
            if not isinstance(base, dict):
                base = {}
        except (TypeError, ValueError):
            base = {}
        base["orb"] = v
        _stats["recorded"] += 1
        if v.get("would_enter"):
            _stats["would_enter"] += 1
        if v.get("reason") == "no_range":
            _stats["no_range"] += 1
        return json.dumps(base, separators=(",", ":"))
    except Exception as e:  # noqa: BLE001
        logger.debug("orb attach failed: %s", e)
        return flow_json


def stats() -> Dict[str, int]:
    return dict(_stats)


def _reset_for_tests() -> None:
    _ranges.clear()
    _bar_ranges.clear()
    for k in _stats:
        _stats[k] = 0
