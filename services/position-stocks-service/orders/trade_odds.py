"""orders/trade_odds.py - E103 (P2 item 13): the odds of this trade's own stop / target, stored on every new position. RECORD ONLY.

What it answers: "with this stop % and this target %, how often did the target come first on THIS stock's own bars today?" A plain formula cannot
say (a driftless walk gives p_target = 1/(1+RR), exactly break-even, by construction), so it replays the stock's 1-minute bars so far today
(feed/bar_store): every earlier minute is a pretend entry at that bar's close, and the stop / target are walked forward bar by bar. A bar that
touches both counts as a STOP (the cautious call). Longs and shorts use the mirrored levels. Entries that reach neither level before the last bar
are counted as "open" and left out of the odds.

Stored under flow_json["odds"]: p_target, p_stop, p_open, n_resolved, break-even win rate 1/(1+RR), average R (gross of charges), a one-line verdict.
Honest limits, also in the verdict text: one stock, one day, overlapping starts (not independent), no charges, resolved entries only (a late
start has little time left, so the odds lean toward quick moves). Early in the day there are too few bars; then only the counts are stored.
Nothing here blocks, sizes or orders anything. TRADE_ODDS_MODE=off stops it. Never raises; stdlib only.
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any, Dict, List, Optional, Sequence

import config

logger = logging.getLogger("position-stocks-trade-odds")

VERSION = 1
MIN_RESOLVED = 20          # fewer resolved pretend entries than this: counts only, no verdict
MARGIN_PTS = 3.0           # within this many points of break-even the verdict says "near break-even"
_stats: Dict[str, int] = {"recorded": 0, "too_few": 0, "no_bars": 0, "errors": 0}


def _f(v) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _off() -> bool:
    return str(getattr(config, "TRADE_ODDS_MODE", "shadow") or "shadow").lower() == "off"


def _clean(bars: Sequence[Any]) -> List[tuple]:
    """(high, low, close) per usable bar, in order. A bar is (t, o, h, l, c, v); anything unreadable or with high < low is dropped. Pure."""
    out: List[tuple] = []
    for b in bars or ():
        try:
            h, l, c = _f(b[2]), _f(b[3]), _f(b[4])
        except (TypeError, IndexError, KeyError):
            continue
        if h is None or l is None or c is None or c <= 0 or l <= 0 or h < l:
            continue
        out.append((h, l, c))
    return out


def simulate(bars: Sequence[Any], side: str, stop_pct: float, target_pct: float) -> Dict[str, int]:
    """Replay: every bar's close is a pretend entry; walk the LATER bars until the stop or the target is touched.
    A bar touching both counts as the stop. The last bar has no later bar, so it is never an entry. Returns {"target", "stop", "open"}. Pure."""
    short = str(side or "").upper() == "SHORT"
    data = _clean(bars)
    sp, tp = float(stop_pct) / 100.0, float(target_pct) / 100.0
    res = {"target": 0, "stop": 0, "open": 0}
    n = len(data)
    for i in range(n - 1):
        entry = data[i][2]
        if short:
            stop, target = entry * (1.0 + sp), entry * (1.0 - tp)
        else:
            stop, target = entry * (1.0 - sp), entry * (1.0 + tp)
        outcome = "open"
        for j in range(i + 1, n):
            hi, lo = data[j][0], data[j][1]
            hit_stop = hi >= stop if short else lo <= stop
            hit_target = lo <= target if short else hi >= target
            if hit_stop:                       # stop first, also when the same bar touches both
                outcome = "stop"
                break
            if hit_target:
                outcome = "target"
                break
        res[outcome] += 1
    return res


def evaluate(bars: Sequence[Any], side: str, stop_pct: Any, target_pct: Any) -> Optional[Dict[str, Any]]:
    """The odds dict for flow_json["odds"], or None when there is nothing to say (no bars, bad levels). Pure."""
    sp, tp = _f(stop_pct), _f(target_pct)
    if sp is None or tp is None or sp <= 0 or tp <= 0:
        return None
    data = _clean(bars)
    if len(data) < 2:
        return None
    r = simulate(bars, side, sp, tp)
    resolved = r["target"] + r["stop"]
    total = resolved + r["open"]
    rr = tp / sp
    be = 1.0 / (1.0 + rr)
    out: Dict[str, Any] = {"v": VERSION, "side": "SHORT" if str(side or "").upper() == "SHORT" else "LONG", "bars": len(data),
                           "n_target": r["target"], "n_stop": r["stop"], "n_open": r["open"], "n_resolved": resolved,
                           "rr": round(rr, 3), "be_win": round(be, 4)}
    if resolved < MIN_RESOLVED:
        out["verdict"] = f"not enough bars yet ({resolved} resolved replays, {MIN_RESOLVED} needed): odds not stated"
        return out
    p_t, p_s = r["target"] / resolved, r["stop"] / resolved
    avg_r = p_t * rr - p_s
    diff = (p_t - be) * 100.0
    out.update(p_target=round(p_t, 4), p_stop=round(p_s, 4), p_open=round(r["open"] / total, 4) if total else None, avg_r=round(avg_r, 3))
    if abs(diff) <= MARGIN_PTS:
        rel = f"near break-even ({diff:+.0f} pts)"
    elif diff > 0:
        rel = f"{diff:.0f} pts ABOVE break-even"
    else:
        rel = f"{-diff:.0f} pts BELOW break-even"
    out["verdict"] = (f"target first {p_t * 100:.0f}% of {resolved} replays vs {be * 100:.0f}% needed: {rel} "
                      f"(avg {avg_r:+.2f}R before charges; one stock, one day, overlapping starts)")
    return out


def attach(flow_json: Optional[str], symbol: str, side: str, stop_pct: Any, target_pct: Any) -> Optional[str]:
    """flow_json text with the odds merged under "odds"; the input unchanged when nothing was recorded. Reads today's bars from feed/bar_store. Never raises."""
    try:
        if _off():
            return flow_json
        from feed import bar_store
        bars = bar_store.get_bars(str(symbol or "").strip().upper())
        if not bars:
            _stats["no_bars"] += 1
            return flow_json
        v = evaluate(bars, side, stop_pct, target_pct)
        if v is None:
            _stats["no_bars"] += 1
            return flow_json
        try:
            base = json.loads(flow_json) if flow_json else {}
            if not isinstance(base, dict):
                base = {}
        except (TypeError, ValueError):
            base = {}
        base["odds"] = v
        _stats["recorded" if "p_target" in v else "too_few"] += 1
        return json.dumps(base, separators=(",", ":"))
    except Exception as e:  # noqa: BLE001
        _stats["errors"] += 1
        logger.debug("trade odds attach failed: %s", e)
        return flow_json


def stats() -> Dict[str, int]:
    return dict(_stats)


def _reset_for_tests() -> None:
    for k in _stats:
        _stats[k] = 0
