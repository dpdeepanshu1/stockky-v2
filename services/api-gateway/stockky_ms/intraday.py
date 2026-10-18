"""stockky_ms.intraday - intraday (15m / 5m) market structure from recorded 1-minute bars (group 441, WS-15). RECORD / EVIDENCE ONLY.

    aggregate_session(bars_1m, minutes, now=None)   1-minute bars -> N-minute bars aligned to the NSE session (09:15-15:30 IST); a bar still forming is dropped
    session_info(bars_1m)                           per IST day: first/last minute, minute count, coverage 0..1, complete flag
    session_vwap(bars)                              typical-price VWAP of the LAST day in `bars`
    intraday_params(minutes)                        MSParams with the history floors and move-size hypotheses scaled for intraday bars
    intraday_ctx(bars)                              the `ctx` analyze() takes: ema20 over the bars, session vwap
    analyze_intraday(bars_1m, minutes, now, ...)    the whole chain -> {tf_min, bars, days, quality, summary (i15_* / i5_*), block (optional)}
    replay_intraday(bars_n, minutes, step)          the engine's replay over N-minute bars with NO lookahead (the evidence primitive)
    alignment(daily_trend, intraday_trend)          aligned_bull | aligned_bear | conflict | None

analyze() only sees a list of bars, so the same detectors run on any bar size. What changes for intraday: bars are built on the SESSION clock (a bucket
never straddles 09:15 or the close), the history floors / move-size thresholds get intraday values, and the overnight gap is a bar-to-bar jump. All values
are HYPOTHESES: params_hash() differs from the daily one, so an intraday row is never mixed up with a daily row, and nothing in the entry path reads it.
Keys are prefixed `i15_` / `i5_` instead of `ms_`. Pure stdlib; no I/O, no clock (`now` is an argument), no env.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .engine import analyze
from .ms_types import C, H, IST_OFFSET_S, L, O, T, V, finite
from .params import MSParams
from .replay import EMA_PERIOD, ema_last, replay
from .summary import flatten_summary

SESSION_OPEN_S = 9 * 3600 + 15 * 60
SESSION_MINUTES = 375
SESSION_LEN_S = SESSION_MINUTES * 60
COMPLETE_FIRST_MINUTE_MAX = 5
COMPLETE_MIN_COVERAGE = 0.80

INTRADAY_PRESETS: Dict[int, Dict[str, float]] = {
    15: {"min_bars": 50, "full_bars": 100, "objective_pct": 1.5, "fib_min_leg_pct": 1.0, "gap_event_pct": 3.0},
    5: {"min_bars": 75, "full_bars": 150, "objective_pct": 1.0, "fib_min_leg_pct": 0.8, "gap_event_pct": 3.0},
}
SUPPORTED_TF = tuple(sorted(INTRADAY_PRESETS))
Bar6 = Tuple[float, float, float, float, float, float]


def tf_prefix(minutes: int) -> str:
    return f"i{int(minutes)}_"


def session_open_ts(ts: float) -> float:
    """Epoch second of 09:15 IST on the IST calendar day that `ts` falls on."""
    day = int((float(ts) + IST_OFFSET_S) // 86400)
    return day * 86400.0 - IST_OFFSET_S + SESSION_OPEN_S


def _clean_minutes(bars_1m: Sequence[Sequence[float]]) -> List[Bar6]:
    out: Dict[float, Bar6] = {}
    for b in bars_1m or []:
        try:
            if len(b) < 5 or not all(finite(b[i]) for i in (T, O, H, L, C)):
                continue
            t, o, h, l, c = (float(b[i]) for i in (T, O, H, L, C))
            v = float(b[V]) if len(b) > V and finite(b[V]) and float(b[V]) >= 0 else 0.0
        except (TypeError, ValueError):
            continue
        if o <= 0 or h <= 0 or l <= 0 or c <= 0 or h < l:
            continue
        m = float(int(t // 60) * 60)
        out[m] = (m, o, h, l, c, v)                       # one bar per minute; the later row wins
    return [out[k] for k in sorted(out)]


def aggregate_session(bars_1m: Sequence[Sequence[float]], minutes: int = 15, now: Optional[float] = None) -> List[Bar6]:
    """N-minute bars on the session clock. A minute outside 09:15-15:30 IST is dropped. Bar t = bucket start. With `now`, an unfinished bucket is dropped."""
    n = max(1, int(minutes))
    width = n * 60.0
    buckets: Dict[Tuple[int, int], List[Bar6]] = {}
    for b in _clean_minutes(bars_1m):
        rel = b[0] - session_open_ts(b[0])
        if rel < 0 or rel >= SESSION_LEN_S:
            continue
        buckets.setdefault((int((b[0] + IST_OFFSET_S) // 86400), int(rel // width)), []).append(b)
    out: List[Bar6] = []
    for (day, k) in sorted(buckets):
        grp = buckets[(day, k)]
        start = day * 86400.0 - IST_OFFSET_S + SESSION_OPEN_S + k * width
        if now is not None and float(now) < start + width:
            continue
        out.append((start, grp[0][O], max(x[H] for x in grp), min(x[L] for x in grp), grp[-1][C], sum(x[V] for x in grp)))
    return out


def session_info(bars_1m: Sequence[Sequence[float]]) -> Dict[int, Dict[str, Any]]:
    """Per IST day number: {first_ts, last_ts, minutes, coverage, complete}. complete = first minute within 5 min of the open and >= 80 % of 375 minutes."""
    days: Dict[int, List[float]] = {}
    for b in _clean_minutes(bars_1m):
        rel = b[0] - session_open_ts(b[0])
        if 0 <= rel < SESSION_LEN_S:
            days.setdefault(int((b[0] + IST_OFFSET_S) // 86400), []).append(b[0])
    out: Dict[int, Dict[str, Any]] = {}
    for d, ts in days.items():
        o_ts = d * 86400.0 - IST_OFFSET_S + SESSION_OPEN_S
        cov = len(ts) / float(SESSION_MINUTES)
        out[d] = {"first_ts": min(ts), "last_ts": max(ts), "minutes": len(ts), "coverage": round(cov, 3),
                  "complete": bool((min(ts) - o_ts) <= COMPLETE_FIRST_MINUTE_MAX * 60 and cov >= COMPLETE_MIN_COVERAGE)}
    return out


def session_vwap(bars: Sequence[Sequence[float]]) -> Optional[float]:
    """Typical-price VWAP of the last IST day present in `bars`. None without volume."""
    if not bars:
        return None
    day = int((float(bars[-1][T]) + IST_OFFSET_S) // 86400)
    pv = vv = 0.0
    for b in reversed(bars):
        if int((float(b[T]) + IST_OFFSET_S) // 86400) != day:
            break
        v = float(b[V]) if len(b) > V and finite(b[V]) and float(b[V]) > 0 else 0.0
        pv += (float(b[H]) + float(b[L]) + float(b[C])) / 3.0 * v
        vv += v
    return pv / vv if vv > 0 else None


def intraday_params(minutes: int = 15, base: Optional[MSParams] = None) -> MSParams:
    """The daily params with the intraday floors and move sizes (unknown timeframe -> the 15-minute preset)."""
    preset = INTRADAY_PRESETS.get(int(minutes), INTRADAY_PRESETS[15])
    b = base or MSParams()
    return dataclasses.replace(b, **{k: type(getattr(b, k))(v) for k, v in preset.items()})


def intraday_ctx(bars: Sequence[Sequence[float]]) -> Dict[str, Any]:
    """analyze() context for intraday bars: ema20 and the session VWAP. A missing value is absent (never read as good news)."""
    ctx: Dict[str, Any] = {}
    e = ema_last([b[C] for b in bars], EMA_PERIOD)
    if e is not None:
        ctx["ema20"] = e
    vw = session_vwap(bars)
    if vw is not None:
        ctx["vwap"] = vw
    return ctx


def prefix_summary(summary: Dict[str, Any], minutes: int) -> Dict[str, Any]:
    """ms_trend -> i15_trend and so on (ms_ver / ms_hash / ms_asof keep their meaning under the new prefix)."""
    pre = tf_prefix(minutes)
    return {(pre + k[3:] if k.startswith("ms_") else pre + k): v for k, v in (summary or {}).items()}


def alignment(daily_trend: Optional[str], intraday_trend: Optional[str]) -> Optional[str]:
    if daily_trend not in ("bullish", "bearish") or intraday_trend not in ("bullish", "bearish"):
        return None
    if daily_trend == intraday_trend:
        return "aligned_bull" if daily_trend == "bullish" else "aligned_bear"
    return "conflict"


def analyze_intraday(bars_1m: Sequence[Sequence[float]], minutes: int = 15, now: Optional[float] = None, params: Optional[MSParams] = None,
                     keep_block: bool = False) -> Dict[str, Any]:
    """1-minute bars (ascending, one or more days) -> structure of the last finished N-minute bar. Never raises."""
    n = int(minutes)
    out: Dict[str, Any] = {"tf_min": n, "bars": 0, "days": 0, "last_bar_ts": None, "quality": "unavailable", "summary": {}}
    try:
        agg = aggregate_session(bars_1m, n, now)
        out["bars"] = len(agg)
        out["days"] = len({int((b[T] + IST_OFFSET_S) // 86400) for b in agg})
        out["last_bar_ts"] = agg[-1][T] if agg else None
        blk = analyze(agg, params=params or intraday_params(n), ctx=intraday_ctx(agg))
        out["quality"] = blk.get("quality") or "unavailable"
        out["summary"] = prefix_summary(flatten_summary(blk), n)
        if keep_block:
            out["block"] = blk
    except Exception as e:  # noqa: BLE001 - evidence code must never take a caller down
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return out


def replay_intraday(bars_n: Sequence[Sequence[float]], minutes: int = 15, step: int = 1, params: Optional[MSParams] = None,
                    warmup: Optional[int] = None) -> List[Dict[str, Any]]:
    """The engine's replay over already-aggregated N-minute bars: at index i it sees bars[:i+1] only. Rows carry the PREFIXED summary."""
    p = params or intraday_params(minutes)
    rows = replay(bars_n, params=p, warmup=warmup, step=step, ctx_fn=intraday_ctx)
    for r in rows:
        r["summary"] = prefix_summary(r["summary"], minutes)
    return rows
