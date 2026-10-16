"""exit_engine/structure_exit.py - daily market structure WHILE A POSITION IS OPEN (group 365, 2026-10-14).

The entry gate asks "is this a good place to buy?". This asks the other half: "has the structure that justified the buy broken?"
It reads the same flat `ms_*` summary (technical service, via entry_engine.structure_gate cache) and can only ever make the
position SAFER:

  * bearish signal  = a fresh bearish CHOCH (STRUCTURE_EXIT_CHOCH_BARS) or a fresh, not-failed bearish BOS (STRUCTURE_EXIT_BOS_BARS)
                      that formed on a daily bar AFTER the entry day (a break that pre-dates the entry is not news).
  * tighten         = position in profit + bearish signal -> stop raised to at least breakeven and to LTP - STRUCTURE_EXIT_TIGHT_ATR x ATR.
  * exit            = (mode `exit` only) position in profit + bearish signal -> sell.
  * structure trail = (STRUCTURE_TRAIL_ENABLED) the stop follows the newest structural higher-low / demand zone (ms_stop) once that level is
                      at / above entry and at least STRUCTURE_TRAIL_MIN_ATR below the price.

Ratchet only: a stop is never lowered. A losing position is never touched (its own stop already protects it). No / stale / insufficient /
split-affected structure = no opinion (same rules as the entry gate). Pure, never raises; the caller applies the verdict.

STRUCTURE_EXIT_MODE: off | log (record only; default since group 415) | tighten | exit (the default from group 399 to 414).
"""
from __future__ import annotations

import logging
import math
from collections import deque
from datetime import date, datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional

import config

logger = logging.getLogger("real-trade-structure-exit")

MODES = ("off", "log", "tighten", "exit")
_IST = timezone(timedelta(hours=5, minutes=30))

_stats: Dict[str, Any] = {"evaluated": 0, "no_opinion": 0, "bearish": 0, "tightened": 0, "trailed": 0, "exited": 0, "log_only": 0, "errors": 0}
_ring: Deque[Dict[str, Any]] = deque(maxlen=60)


def _f(v) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _pos(v) -> Optional[float]:
    x = _f(v)
    return x if x is not None and x > 0 else None


def mode() -> str:
    m = str(getattr(config, "STRUCTURE_EXIT_MODE", "log") or "log").strip().lower()
    return m if m in MODES else "log"


def _to_date(v) -> Optional[date]:
    """A date from '2026-10-09', an ISO datetime, or epoch seconds / ms. None when unparseable."""
    try:
        if v is None:
            return None
        if isinstance(v, datetime):
            d = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
            return d.astimezone(_IST).date()
        if isinstance(v, date):
            return v
        if isinstance(v, (int, float)):
            ts = float(v)
            ts = ts / 1000.0 if ts > 1e11 else ts
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_IST).date()
        s = str(v).strip()
        if len(s) == 10:
            return date.fromisoformat(s)
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(_IST).date()
    except Exception:  # noqa: BLE001
        return None


def sessions_after(entry_day: Optional[date], asof_day: Optional[date]) -> int:
    """Weekdays in (entry_day, asof_day] = daily bars that closed AFTER the entry day. Holidays are not subtracted (that can only
    over-count by a bar, and the signal windows are 3 bars wide). 0 when either date is missing or asof is not after entry."""
    if entry_day is None or asof_day is None or asof_day <= entry_day:
        return 0
    n, d = 0, entry_day
    while d < asof_day:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _no_data(summary: Optional[Dict[str, Any]], ltp) -> Optional[str]:
    from entry_engine import structure_gate
    return structure_gate.no_data_reason(summary, ltp)


def bearish_signals(summary: Dict[str, Any], closed_after_entry: int) -> List[Dict[str, Any]]:
    """Fresh bearish CHOCH / BOS that formed on a bar after the entry day. bars_since counts from the last closed bar (0 = that bar)."""
    out: List[Dict[str, Any]] = []
    ch_bars = _f(summary.get("ms_choch_bars"))
    if summary.get("ms_choch_dir") == "bearish" and ch_bars is not None and ch_bars >= 0:
        if ch_bars <= int(config.STRUCTURE_EXIT_CHOCH_BARS) and ch_bars < closed_after_entry:
            out.append({"kind": "bearish_choch", "bars_since": int(ch_bars)})
    bos_bars = _f(summary.get("ms_bos_bars"))
    if summary.get("ms_bos_dir") == "bearish" and not summary.get("ms_bos_failed") and bos_bars is not None and bos_bars >= 0:
        if bos_bars <= int(config.STRUCTURE_EXIT_BOS_BARS) and bos_bars < closed_after_entry:
            out.append({"kind": "bearish_bos", "bars_since": int(bos_bars)})
    return out


def evaluate(summary: Optional[Dict[str, Any]], *, entry_price, ltp, current_stop, atr=None, opened_at=None,
             exit_mode: Optional[str] = None) -> Dict[str, Any]:
    """Pure verdict for ONE open long position.

    Keys: opinion ('ok'|'no_opinion'|'off'), mode, action ('none'|'tighten'|'trail'|'exit'), new_stop (only for tighten / trail), signals[],
    in_profit, reasons[], no_data. Never raises."""
    m = exit_mode if exit_mode in MODES else mode()
    base: Dict[str, Any] = {"opinion": "no_opinion", "mode": m, "action": "none", "new_stop": None, "signals": [], "in_profit": None,
                            "reasons": [], "no_data": None, "closed_after_entry": None, "trail_candidate": None}
    try:
        if m == "off":
            base["opinion"] = "off"
            return base
        why = _no_data(summary, ltp)
        if why:
            base["no_data"] = why
            return base
        entry, px = _pos(entry_price), _pos(ltp)
        if entry is None or px is None:
            base["no_data"] = "no entry / live price"
            return base
        atr_v = _pos(atr) or _pos((summary or {}).get("ms_atr"))
        stop = _f(current_stop)
        base["opinion"] = "ok"
        base["in_profit"] = px > entry
        n_after = sessions_after(_to_date(opened_at), _to_date((summary or {}).get("ms_asof")))
        base["closed_after_entry"] = n_after
        sigs = bearish_signals(summary or {}, n_after)
        base["signals"] = sigs

        new_stop: Optional[float] = None
        action = "none"
        # 1) bearish structure while in profit -> tighten (never on a loser)
        if sigs and base["in_profit"] and m in ("tighten", "exit"):
            cand = max(entry, px - float(config.STRUCTURE_EXIT_TIGHT_ATR) * atr_v) if atr_v is not None else entry
            cand = round(min(cand, px * 0.9995), 2)           # always strictly below the price
            if stop is None or cand > stop + 0.004:
                new_stop, action = cand, "tighten"
            base["reasons"].append("bearish daily structure after entry: " + ", ".join(f"{s['kind']} {s['bars_since']} bar(s) ago" for s in sigs))
        # 2) structure trail: follow the newest structural higher-low once it is at / above entry and clear of the price
        if (action == "none" and m in ("tighten", "exit") and bool(config.STRUCTURE_TRAIL_ENABLED) and base["in_profit"]
                and atr_v is not None and not sigs):
            lvl = _pos((summary or {}).get("ms_stop"))
            base["trail_candidate"] = lvl
            if lvl is not None and lvl >= entry and (px - lvl) >= float(config.STRUCTURE_TRAIL_MIN_ATR) * atr_v and (stop is None or lvl > stop + 0.004):
                new_stop, action = round(lvl, 2), "trail"
                base["reasons"].append(f"stop follows the structural higher-low / demand zone at {lvl:.2f}")
        # 3) exit mode: sell an in-profit position on a fresh bearish signal
        if sigs and base["in_profit"] and m == "exit":
            action = "exit"
        base["action"], base["new_stop"] = action, new_stop
        return base
    except Exception as e:  # noqa: BLE001 - the exit loop must never break on a structure read
        base["opinion"], base["action"], base["new_stop"] = "no_opinion", "none", None
        base["no_data"] = f"error: {str(e)[:80]}"
        return base


def record(symbol: str, trade_mode: str, verdict: Dict[str, Any], applied: bool, ltp=None) -> None:
    """Process counters + a ring of recent verdicts for the status endpoint. Never raises."""
    try:
        _stats["evaluated"] += 1
        if verdict.get("opinion") != "ok":
            _stats["no_opinion"] += 1
            return
        sigs = verdict.get("signals") or []
        if sigs:
            _stats["bearish"] += 1
        act = verdict.get("action")
        if act == "tighten" and applied:
            _stats["tightened"] += 1
        elif act == "trail" and applied:
            _stats["trailed"] += 1
        elif act == "exit" and applied:
            _stats["exited"] += 1
        elif sigs or act != "none":
            _stats["log_only"] += 1
        if sigs or act != "none":
            _ring.appendleft({"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "symbol": symbol, "mode": str(trade_mode).upper(),
                              "action": act, "applied": bool(applied), "signals": [s.get("kind") for s in sigs], "new_stop": verdict.get("new_stop"),
                              "ltp": _f(ltp), "reasons": (verdict.get("reasons") or [])[:3]})
    except Exception:  # noqa: BLE001
        _stats["errors"] += 1


def stats() -> Dict[str, Any]:
    return {"mode": mode(), "trail_enabled": bool(getattr(config, "STRUCTURE_TRAIL_ENABLED", True)), "tight_atr": getattr(config, "STRUCTURE_EXIT_TIGHT_ATR", None),
            "counters": dict(_stats), "recent": list(_ring)[:30]}


def reset() -> None:
    for k in _stats:
        _stats[k] = 0
    _ring.clear()
