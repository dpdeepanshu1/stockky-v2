"""stockky_ms.timeframes - the candle timeframes of the chart: 1m, 5m, 15m, 30m, 1h and 1d.

    TIMEFRAMES                          ordered keys
    spec(tf)                            {key, minutes, fetch, period, label, intraday, tail, unit, note} or None for an unknown key
    rows_to_bars(rows)                  market-data candle rows ({date, open, high, low, close, volume}) -> [(epoch_s, o, h, l, c, v)]
                                        (a date WITHOUT an offset is IST wall-clock time, which is what yfinance / Dhan rows carry)
    session_bars(rows, tf, now)         rows -> {closed, live, labels, live_label, last_base_ts, lag_s} on the NSE session clock (09:15-15:30 IST)
    label_for(ts, tf)                   'YYYY-MM-DD HH:MM' (IST) for intraday, 'YYYY-MM-DD' for 1d
    span_of(pattern_name)               how many candles a candle pattern uses (1, 2 or 3)
    crosses_session(bars, i, span)      True when the `span` candles ending at index i are not all on the same IST day

Pure stdlib; no I/O, no env, no clock (`now` is an argument). Context only: nothing here feeds a decision, gate, score or ML row.

WHY a second aggregator (intraday.aggregate_session already exists): that one is evidence code (recorder / replay) with its own
contract and tests, and it treats a bucket as unfinished until start + width, which is wrong for the last bucket of the day (the 30-minute
bucket 15:15-15:30 has only 15 minutes, the 60-minute bucket 15:15-15:30 too). Here a bucket ends at min(start + width, 15:30), and the
bar that is still forming is returned separately (`live`) so the chart can draw it while the pattern engine only ever sees closed bars.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .ms_types import C, H, IST_OFFSET_S, L, O, T, V, finite

SESSION_OPEN_S = 9 * 3600 + 15 * 60          # 09:15 IST, seconds after IST midnight
SESSION_LEN_S = 375 * 60                     # 09:15 -> 15:30

TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "1d")

# key -> minutes, the market-data `interval` to ask for, the `period` to ask for, how many bars the chart payload carries by default.
# 30m is built from 15m bars (Dhan has no 30m, and a 15m base keeps the buckets on the session clock). 1m asks for 5 sessions
# (yfinance keeps 7 days of 1-minute data), 5m / 15m for a month, 1h for 3 months.
_SPECS: Dict[str, Dict[str, Any]] = {
    "1m": {"minutes": 1, "fetch": "1m", "period": "5d", "label": "1 min", "tail": 750},
    "5m": {"minutes": 5, "fetch": "5m", "period": "1mo", "label": "5 min", "tail": 600},
    "15m": {"minutes": 15, "fetch": "15m", "period": "1mo", "label": "15 min", "tail": 400},
    "30m": {"minutes": 30, "fetch": "15m", "period": "1mo", "label": "30 min", "tail": 260},
    "1h": {"minutes": 60, "fetch": "1h", "period": "3mo", "label": "1 hour", "tail": 300},
    "1d": {"minutes": 1440, "fetch": "1d", "period": "1y", "label": "Daily", "tail": 300},
}
_FETCH_MIN = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}
MAX_TAIL = 1500
_NOTES = {
    "1m": "1-minute candles are very noisy: patterns on them fail more often than on 15-minute, hourly or daily candles.",
    "5m": "5-minute candles are noisy: treat patterns here as a short-term read and check the 15-minute or hourly chart.",
}


def spec(tf: Any) -> Optional[Dict[str, Any]]:
    key = str(tf or "").strip().lower()
    key = {"60m": "1h", "d": "1d", "day": "1d", "daily": "1d"}.get(key, key)
    s = _SPECS.get(key)
    if s is None:
        return None
    return {"key": key, "intraday": key != "1d", "unit": "day" if key == "1d" else "candle", "note": _NOTES.get(key), **s}


def norm_tf(tf: Any, default: str = "1d") -> str:
    s = spec(tf)
    return s["key"] if s else default


def clamp_tail(tf: Any, tail: Any = None) -> int:
    """The tail to use: the requested one clamped to [30, MAX_TAIL] (500 for daily, as before), or the timeframe's default."""
    s = spec(tf) or _SPECS["1d"]
    cap = 500 if (spec(tf) or {}).get("key", "1d") == "1d" else MAX_TAIL
    try:
        t = int(tail) if tail is not None else int(s["tail"])
    except (TypeError, ValueError):
        t = int(s["tail"])
    return max(30, min(t, cap))


# ---------------------------------------------------------------- time helpers
def _ist(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts) + IST_OFFSET_S, tz=timezone.utc)


def label_for(ts: float, tf: Any) -> str:
    d = _ist(ts)
    return d.strftime("%Y-%m-%d") if not (spec(tf) or {}).get("intraday") else d.strftime("%Y-%m-%d %H:%M")


def _day_no(ts: float) -> int:
    return int((float(ts) + IST_OFFSET_S) // 86400)


def _open_ts(day_no: int) -> float:
    return day_no * 86400.0 - IST_OFFSET_S + SESSION_OPEN_S


def parse_row_ts(v: Any) -> Optional[float]:
    """Epoch seconds from a candle row's date: epoch (s or ms), or an ISO string. No offset in the string = IST wall-clock."""
    try:
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            x = float(v)
            return x / 1000.0 if x > 1e11 else x
        s = str(v).strip().replace("Z", "+00:00")
        if not s:
            return None
        if len(s) == 10:
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() - IST_OFFSET_S    # 00:00 IST of that date
        dt = datetime.fromisoformat(s.replace(" ", "T", 1) if " " in s and "T" not in s else s)
        if dt.tzinfo is None:
            return (dt.replace(tzinfo=timezone.utc) - timedelta(seconds=IST_OFFSET_S)).timestamp()
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        return None


def rows_to_bars(rows: Any) -> List[Tuple[float, float, float, float, float, float]]:
    """Candle rows -> ascending (t, o, h, l, c, v), one per timestamp (the later row wins); bad rows are dropped, never raised."""
    out: Dict[float, Tuple[float, float, float, float, float, float]] = {}
    for r in rows or []:
        try:
            if not isinstance(r, dict):
                continue
            ts = parse_row_ts(r.get("date") if r.get("date") is not None else (r.get("t") if r.get("t") is not None else r.get("timestamp")))
            o, h, l, c = r.get("open"), r.get("high"), r.get("low"), r.get("close")
            if ts is None or not all(finite(x) for x in (h, l, c)):
                continue
            cf = float(c)
            of = float(o) if finite(o) else cf
            hf, lf = float(h), float(l)
            if cf <= 0 or hf <= 0 or lf <= 0 or hf < lf:
                continue
            v = r.get("volume")
            out[ts] = (ts, of, hf, lf, cf, float(v) if finite(v) and float(v) >= 0 else 0.0)
        except Exception:  # noqa: BLE001
            continue
    return [out[k] for k in sorted(out)]


# ---------------------------------------------------------------- aggregation
def _bucket(base: Sequence[Sequence[float]], minutes: int) -> List[Tuple[float, float, float, float, float, float, float]]:
    """Base bars -> N-minute buckets on the session clock: (start, o, h, l, c, v, end). A base bar outside 09:15-15:30 is dropped."""
    width = max(1, int(minutes)) * 60.0
    groups: Dict[Tuple[int, int], List[Sequence[float]]] = {}
    for b in base:
        rel = b[T] - _open_ts(_day_no(b[T]))
        if rel < 0 or rel >= SESSION_LEN_S:
            continue
        groups.setdefault((_day_no(b[T]), int(rel // width)), []).append(b)
    out = []
    for (day, k) in sorted(groups):
        g = groups[(day, k)]
        start = _open_ts(day) + k * width
        end = min(start + width, _open_ts(day) + SESSION_LEN_S)
        out.append((start, g[0][O], max(x[H] for x in g), min(x[L] for x in g), g[-1][C], sum(x[V] for x in g), end))
    return out


def session_bars(rows: Any, tf: Any, now: Optional[float] = None) -> Dict[str, Any]:
    """Candle rows -> the closed bars (what patterns see) and the still-forming bar (what the chart may draw).

    Daily: one bar per IST date; a bar dated `now`'s own IST day is forming until 15:30, so the closed list ends yesterday while the market
    is open. Intraday: N-minute buckets; the last bucket that has not reached its end at `now` is `live`. now=None treats every bucket as
    closed. lag_s = how long ago the newest base bar ended (None for daily / no data): a large value while the market is open means the
    data source is behind.
    """
    s = spec(tf)
    out: Dict[str, Any] = {"closed": [], "labels": [], "live": None, "live_label": None, "last_base_ts": None, "lag_s": None}
    if s is None:
        return out
    base = rows_to_bars(rows)
    if not base:
        return out
    out["last_base_ts"] = base[-1][T]
    if not s["intraday"]:
        days: Dict[int, List[Sequence[float]]] = {}
        for b in base:
            days[_day_no(b[T])] = [b]                      # a daily row IS the bar; the later duplicate wins
        bars = [days[d][0] for d in sorted(days)]
        live = None
        if now is not None and bars and _day_no(bars[-1][T]) == _day_no(now) and float(now) < _open_ts(_day_no(now)) + SESSION_LEN_S:
            live = bars.pop()
        out["closed"] = [(b[T], b[O], b[H], b[L], b[C], b[V]) for b in bars]
        out["labels"] = [label_for(b[T], "1d") for b in bars]
        if live is not None:
            out["live"] = (live[T], live[O], live[H], live[L], live[C], live[V])
            out["live_label"] = label_for(live[T], "1d")
        return out
    base_min = _FETCH_MIN.get(s["fetch"], 1)
    buckets = _bucket(base, s["minutes"])
    closed, live = [], None
    for k, bk in enumerate(buckets):
        if now is not None and float(now) < bk[6] and k == len(buckets) - 1:
            live = bk
        else:
            closed.append(bk)
    out["closed"] = [(b[0], b[1], b[2], b[3], b[4], b[5]) for b in closed]
    out["labels"] = [label_for(b[0], tf) for b in closed]
    if live is not None:
        out["live"] = (live[0], live[1], live[2], live[3], live[4], live[5])
        out["live_label"] = label_for(live[0], tf)
    if now is not None:
        out["lag_s"] = max(0, int(float(now) - (base[-1][T] + base_min * 60.0)))
    return out


# ---------------------------------------------------------------- candle patterns across a session break
_THREE = {"morning_star", "evening_star", "three_white_soldiers", "three_black_crows"}
_TWO = {"bullish_engulfing", "bearish_engulfing", "bullish_harami", "bearish_harami", "piercing_line", "dark_cloud_cover", "tweezer_bottom", "tweezer_top"}


def span_of(pattern_name: str) -> int:
    return 3 if pattern_name in _THREE else 2 if pattern_name in _TWO else 1


def crosses_session(bars: Sequence[Sequence[float]], i: int, span: int) -> bool:
    """True when the `span` candles ending at index i are not all on one IST day (an overnight gap is not a pattern on a 5-minute chart)."""
    try:
        if span <= 1 or i - span + 1 < 0:
            return False
        d = _day_no(bars[i][T])
        return any(_day_no(bars[j][T]) != d for j in range(i - span + 1, i))
    except Exception:  # noqa: BLE001
        return False
