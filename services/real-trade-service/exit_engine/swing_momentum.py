"""exit_engine/swing_momentum.py - momentum health for a SWING position on 5-minute bars during the session (E274 round 48; plan: "momentum health on 5-minute bars").

The shared pure decision (exit_engine/momentum_health.py, identical copy in Position Stocks) is fed today's 1-minute bars (entry_engine.orb_bars cache, never waits for the network) rolled up
into 5-minute bars, from the later of the entry time and the 09:15 open. Real Trade has no tape and no VWAP-fade feed, so the signals that can fire are no_new_best (time without a new high),
climax_bar (volume spike with a wick on the profit side) and at_wall (gain reached the typical gain); a BOOK needs 3 of them, so it is rare and always in profit by SWING_MOMENTUM_MIN_PROFIT_PCT.
Only ever makes the trade safer: the stop is raised (never lowered), or the position is sold in profit. Bars missing / stale / too few = no opinion. Pure, never raises.

SWING_MOMENTUM_EXIT_MODE: off | tighten | exit (default exit).
"""
from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional

import config
from exit_engine import momentum_health

_IST = timezone(timedelta(hours=5, minutes=30))
MODES = ("off", "tighten", "exit")
_stats: Dict[str, Any] = {"evaluated": 0, "no_bars": 0, "hold": 0, "tighten": 0, "book": 0, "errors": 0}
_ring: Deque[Dict[str, Any]] = deque(maxlen=60)
_last: Dict[int, float] = {}


def mode() -> str:
    m = str(getattr(config, "SWING_MOMENTUM_EXIT_MODE", "exit") or "exit").strip().lower()
    return m if m in MODES else "exit"


def to_5m(bars: Any, start_ts: float, now_ts: float) -> List[tuple]:
    """1-minute rows (t, o, h, l, c, v; t = epoch seconds of the bar start) -> 5-minute rows, only COMPLETE 5-minute buckets that start at or after `start_ts` and end before `now_ts`."""
    buckets: Dict[int, list] = {}
    for b in bars or ():
        try:
            t, o, h, l, c, v = (float(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5]))
        except (TypeError, ValueError, IndexError):
            continue
        if not all(math.isfinite(x) for x in (t, o, h, l, c, v)) or t < start_ts or h < l or l <= 0:
            continue
        buckets.setdefault(int(t // 300), []).append((t, o, h, l, c, v))
    out: List[tuple] = []
    for k in sorted(buckets):
        rows = sorted(buckets[k])
        if (k + 1) * 300 > now_ts or len(rows) < 3:        # still forming, or too thin to be a bar
            continue
        out.append((k * 300.0, rows[0][1], max(r[2] for r in rows), min(r[3] for r in rows), rows[-1][4], sum(r[5] for r in rows)))
    return out


def _row_ok(r: Any) -> bool:
    try:
        return len(r) >= 6 and all(math.isfinite(float(x)) for x in r[:6])
    except (TypeError, ValueError):
        return False


def evaluate(*, bars_1m: Any, entry, stop, ltp, qty, opened_at: Any, now: datetime, tick: float = 0.05, mode_: Optional[str] = None) -> Dict[str, Any]:
    """{"action": "HOLD"|"TIGHTEN"|"BOOK", "new_stop", "signals", "reason"}. Never raises."""
    hold: Dict[str, Any] = {"action": "HOLD", "new_stop": None, "signals": [], "reason": ""}
    try:
        _stats["evaluated"] += 1
        m = mode_ or mode()
        if m == "off":
            return {**hold, "reason": "mode off"}
        now_ist = now.astimezone(_IST)
        day_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0).timestamp()
        opened_ts = opened_at.timestamp() if isinstance(opened_at, datetime) else 0.0
        start = max(day_open, opened_ts)
        bars = to_5m(bars_1m, start, now.timestamp())
        if len(bars) < int(getattr(config, "SWING_MOMENTUM_MIN_BARS", 6)):
            _stats["no_bars"] += 1
            return {**hold, "reason": "too few 5-minute bars"}
        if now.timestamp() - bars[-1][0] > 900:               # newest complete bar older than 15 min: stale cache, no opinion
            _stats["no_bars"] += 1
            return {**hold, "reason": "bars are stale"}
        # momentum_health.decide ignores the LAST row (a bar still forming): hand it the forming bucket (or a flat zero-volume stand-in) so the last COMPLETE bar is judged
        nb = (int(now.timestamp() // 300)) * 300.0
        part = [r for r in (bars_1m or ()) if _row_ok(r) and float(r[0]) >= nb]
        if part:
            bars.append((nb, float(part[0][1]), max(float(r[2]) for r in part), min(float(r[3]) for r in part), float(part[-1][4]), sum(float(r[5]) for r in part)))
        else:
            c = bars[-1][4]
            bars.append((bars[-1][0] + 300.0, c, c, c, c, 0.0))
        d = momentum_health.decide(
            side="LONG", entry=entry, stop=stop, target=None, ltp=ltp, qty=qty, bars=bars, tick=tick,
            age_min=(now.timestamp() - start) / 60.0, no_new_best_min=float(getattr(config, "SWING_MOMENTUM_NO_NEW_BEST_MIN", 90.0)),
            scaleout=False, book_n=int(getattr(config, "SWING_MOMENTUM_BOOK_N", 3)),
            min_book_profit_pct=float(getattr(config, "SWING_MOMENTUM_MIN_PROFIT_PCT", 1.0)), gap_min_pct=float(getattr(config, "SWING_MOMENTUM_GAP_MIN_PCT", 0.5)))
        act = d.get("action")
        out = {"action": "HOLD", "new_stop": None, "signals": list(d.get("signals") or []), "reason": str(d.get("reason") or "")[:200]}
        if act == "BOOK" and m == "exit":
            _stats["book"] += 1
            out["action"] = "BOOK"
        elif act in ("TIGHTEN", "BOOK") and d.get("new_stop"):      # tighten mode: a BOOK verdict degrades to the tighter stop it carries
            _stats["tighten"] += 1
            out["action"], out["new_stop"] = "TIGHTEN", float(d["new_stop"])
        else:
            _stats["hold"] += 1
        return out
    except Exception:  # noqa: BLE001
        _stats["errors"] += 1
        return hold


def due(position_id: int, mono: float, every_s: float = 60.0) -> bool:
    """True at most once per `every_s` seconds per position (the exit loop runs every cycle)."""
    if mono - _last.get(position_id, -1e9) < every_s:
        return False
    _last[position_id] = mono
    return True


def record(symbol: str, verdict: Dict[str, Any], applied: bool, ltp: Any) -> None:
    try:
        _ring.append({"symbol": symbol, "action": verdict.get("action"), "new_stop": verdict.get("new_stop"), "signals": verdict.get("signals"), "applied": bool(applied), "ltp": ltp,
                      "reason": verdict.get("reason")})
    except Exception:  # noqa: BLE001
        pass


def stats() -> Dict[str, Any]:
    return {"mode": mode(), **dict(_stats), "recent": list(_ring)[-20:]}


def reset() -> None:
    _ring.clear()
    _last.clear()
    for k in _stats:
        _stats[k] = 0
