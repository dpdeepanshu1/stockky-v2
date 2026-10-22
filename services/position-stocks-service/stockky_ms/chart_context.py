"""stockky_ms.chart_context - E25 / E26: everything the candlestick chart and the pattern / regime badges need, in one call.

    chart_context(raw_bars, dates, tail=300, structure_trend=None, tf="1d", live=None, live_date=None, lag_s=None) -> dict

CONTEXT ONLY, like patterns.py: nothing here feeds a decision, gate, score or ML feature. It turns `analyze_patterns` output into

  bars      the last `tail` CLOSED bars as [label, open, high, low, close, volume] (label = "YYYY-MM-DD" for 1d, "YYYY-MM-DD HH:MM" IST bar start
            for 1m / 5m / 15m / 30m / 1h; every label is unique, so lines and markers address a candle by its label)
  live      the bar that is still forming (same shape) or None; drawn by the chart, never seen by the pattern engine
  tf, unit, note, data_lag_s   timeframe key, "day" | "candle" (wording), a plain caution for 1m / 5m, seconds the data is behind (intraday)
  markers   candle patterns on the last 3 bars, with the date of the bar they sit on
  shapes    chart patterns as drawable lines: {name, bias, status, label, tip, lines: [{role, x1, y1, x2, y2, dashed}], target}
  levels    old-resistance-now-support / old-support-now-resistance prices
  badges    plain-English chips: {key, kind, label, tone, tip, status?, bars_ago?}  (tone: good | bad | warn | mute)
  regime    {trend, volatility, momentum, structure} - one word each, or None when it cannot be read

Pure function of its inputs (no clock, no I/O); each section degrades to empty / None, and the call never raises.
Dates are passed in (not derived from epoch seconds) so a time-zone shift can never move a candle to the wrong session.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .ms_types import C, H, L, O, V, finite, r2
from .patterns import analyze_patterns, _sma
from .timeframes import crosses_session, span_of, spec

SCHEMA = 1
MAX_SHAPES = 3
MAX_BADGES = 14

# name -> (label, one-line meaning). Wording for the guide's 16 candles + 12 chart patterns is the whyXShubh visual guide's;
# the rest (marubozu, harami, piercing line, dark cloud cover, rectangle) follows the same plain style.
CANDLE_INFO: Dict[str, Any] = {
    "hammer": ("Hammer", "Small body near the top, long lower wick. Sellers pushed price down, buyers slammed it back up."),
    "inverted_hammer": ("Inverted hammer", "Small body near the bottom, long upper wick. An early sign buyers are testing higher prices."),
    "bullish_engulfing": ("Bullish engulfing", "A big bullish candle fully swallows the prior small bearish one: buyers have taken control."),
    "morning_star": ("Morning star", "Big drop, a pause, then a strong rally back up: the classic 3-candle bottoming signal."),
    "three_white_soldiers": ("Three white soldiers", "Three consecutive strong bullish candles, each closing higher: steady, broad buying pressure."),
    "tweezer_bottom": ("Tweezer bottom", "Two candles touching almost the same low: sellers tried twice and failed both times."),
    "shooting_star": ("Shooting star", "Small body near the bottom, long upper wick. Buyers reached high, but sellers slapped it back down."),
    "hanging_man": ("Hanging man", "Looks like a hammer, but appears after an uptrend: a warning that sellers are stepping in."),
    "bearish_engulfing": ("Bearish engulfing", "A big bearish candle fully swallows the prior small bullish one: sellers have taken control."),
    "evening_star": ("Evening star", "Big rally, a pause, then a strong drop back down: the classic 3-candle topping signal."),
    "three_black_crows": ("Three black crows", "Three consecutive strong bearish candles, each closing lower: steady, broad selling pressure."),
    "tweezer_top": ("Tweezer top", "Two candles touching almost the same high: buyers tried twice and failed both times."),
    "doji": ("Doji", "Open and close are almost equal: neither side won. Often marks a pause before a move."),
    "dragonfly_doji": ("Dragonfly doji", "Flat top, long lower wick. Sellers drove price down but buyers recovered it fully by the close."),
    "gravestone_doji": ("Gravestone doji", "Flat bottom, long upper wick. Buyers pushed up but gave it all back by the close."),
    "spinning_top": ("Spinning top", "Small body, wicks on both sides of similar length: a tug-of-war with no clear winner."),
    "marubozu": ("Marubozu", "A candle with almost no wicks: one side controlled the whole session."),
    "bullish_harami": ("Bullish harami", "A small bullish candle inside the prior big bearish one: the selling is losing steam."),
    "bearish_harami": ("Bearish harami", "A small bearish candle inside the prior big bullish one: the buying is losing steam."),
    "piercing_line": ("Piercing line", "After a drop, price opens lower but closes above the middle of the prior bearish candle."),
    "dark_cloud_cover": ("Dark cloud cover", "After a rally, price opens higher but closes below the middle of the prior bullish candle."),
}
CHART_INFO: Dict[str, Any] = {
    "double_top": ("Double top", "Two peaks at nearly the same level. A break below the neckline confirms the reversal down."),
    "double_bottom": ("Double bottom", "Two troughs at nearly the same level. A break above the neckline confirms the reversal up."),
    "head_and_shoulders": ("Head & shoulders", "A high, a higher high, then a matching high again. Breaking the neckline signals a top."),
    "inverse_head_and_shoulders": ("Inverse head & shoulders", "A low, a lower low, then a matching low again. Breaking the neckline signals a bottom."),
    "rising_wedge": ("Rising wedge", "Price grinds higher inside two converging, rising lines: often resolves with a sharp breakdown."),
    "falling_wedge": ("Falling wedge", "Price grinds lower inside two converging, falling lines: often resolves with a sharp breakout up."),
    "ascending_triangle": ("Ascending triangle", "A flat ceiling above rising lows: buyers keep stepping in higher, until they break through."),
    "descending_triangle": ("Descending triangle", "A flat floor below falling highs: sellers keep capping it lower, until they break through."),
    "symmetrical_triangle": ("Symmetrical triangle", "Highs and lows squeeze together toward a point. The breakout can go either way: watch for the trigger."),
    "bull_flag": ("Bull flag", "A sharp rally (the pole), then a small down-sloping pause (the flag), then continuation higher."),
    "bear_flag": ("Bear flag", "A sharp drop (the pole), then a small up-sloping pause (the flag), then continuation lower."),
    "cup_and_handle": ("Cup & handle", "A rounded 'cup' recovery, a small dip (the 'handle'), then a breakout to new highs."),
    "rectangle": ("Rectangle", "Price bounces between a flat ceiling and a flat floor. The break out of the range sets the direction."),
}
_TONE = {"bullish": "good", "bearish": "bad", "neutral": "mute"}


def _title(name: str) -> str:
    return str(name).replace("_", " ").capitalize()


def _clean(raw_bars: Any, dates: Any, keep: int = 10):
    """Parallel (bars, dates), keeping only rows with finite OHLC and a usable date string (`keep` chars: 10 = date, 16 = date + HH:MM)."""
    bars: List[Any] = []
    ds: List[str] = []
    try:
        seq_b, seq_d = list(raw_bars or []), list(dates or [])
    except TypeError:
        return bars, ds
    for b, d in zip(seq_b, seq_d):
        try:
            if len(b) >= 6 and all(finite(x) for x in b[:5]) and isinstance(d, str) and len(d) >= keep:
                bars.append((float(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5]) if finite(b[5]) else 0.0))
                ds.append(d[:keep])
        except (TypeError, ValueError):
            continue
    return bars, ds


def _regime_trend(bars, u: str = "day") -> Optional[Dict[str, Any]]:
    closes = [b[C] for b in bars]
    n = len(closes)
    px = closes[-1]
    if n >= 205:
        s50, s200 = _sma(closes, 50, n - 1), _sma(closes, 200, n - 1)
        if s50 is None or s200 is None:
            return None
        if px > s50 > s200:
            return {"word": "up", "label": "Uptrend", "tone": "good", "tip": f"Price is above its 50-{u} average, and the 50-{u} is above the 200-{u} average."}
        if px < s50 < s200:
            return {"word": "down", "label": "Downtrend", "tone": "bad", "tip": f"Price is below its 50-{u} average, and the 50-{u} is below the 200-{u} average."}
        return {"word": "sideways", "label": "No clear trend", "tone": "mute", "tip": f"Price, the 50-{u} and the 200-{u} averages are not lined up in one direction."}
    if n >= 55:
        s20, s50 = _sma(closes, 20, n - 1), _sma(closes, 50, n - 1)
        if s20 is None or s50 is None:
            return None
        short = f" Less than 200 {u}s of history, so this is the short-term read."
        if px > s20 > s50:
            return {"word": "up", "label": "Short-term uptrend", "tone": "good", "tip": f"Price is above its 20-{u} average, and the 20-{u} is above the 50-{u} average." + short}
        if px < s20 < s50:
            return {"word": "down", "label": "Short-term downtrend", "tone": "bad", "tip": f"Price is below its 20-{u} average, and the 20-{u} is below the 50-{u} average." + short}
        return {"word": "sideways", "label": "No clear trend", "tone": "mute", "tip": f"Price and its 20 / 50-{u} averages are not lined up in one direction."}
    return None


def _seg(ds, lo, n, role, x0, y0, x1, y1, dashed=False) -> Optional[Dict[str, Any]]:
    """Line from bar x0 to bar x1, clipped to the displayed window (the price is interpolated at the clip)."""
    if not (finite(y0) and finite(y1)) or x1 <= lo:
        return None
    if x0 < lo:
        y0 = y0 + (y1 - y0) * (lo - x0) / (x1 - x0) if x1 != x0 else y0
        x0 = lo
    return {"role": role, "x1": ds[min(max(x0, 0), n - 1)], "y1": r2(float(y0)), "x2": ds[min(x1, n - 1)], "y2": r2(float(y1)), "dashed": dashed}


def _shape(p: Dict[str, Any], ds, lo, n) -> Optional[Dict[str, Any]]:
    name = p.get("name")
    start = p.get("start")
    if name not in CHART_INFO or not isinstance(start, int):
        return None
    last = n - 1
    lines: List[Optional[Dict[str, Any]]] = []
    if "neckline" in p:
        lines.append(_seg(ds, lo, n, "neckline", start, p["neckline"], last, p["neckline"]))
        if finite(p.get("target")):
            lines.append(_seg(ds, lo, n, "target", max(start, last - 15), p["target"], last, p["target"], dashed=True))
    elif "upper0" in p and "lower0" in p:
        lines.append(_seg(ds, lo, n, "upper", start, p["upper0"], last, p["upper"]))
        lines.append(_seg(ds, lo, n, "lower", start, p["lower0"], last, p["lower"]))
    elif "flag_high" in p:
        lines.append(_seg(ds, lo, n, "upper", start, p["flag_high"], last, p["flag_high"]))
        lines.append(_seg(ds, lo, n, "lower", start, p["flag_low"], last, p["flag_low"]))
        if finite(p.get("target")):
            lines.append(_seg(ds, lo, n, "target", max(start, last - 15), p["target"], last, p["target"], dashed=True))
    lines = [x for x in lines if x]
    if not lines:
        return None
    label, tip = CHART_INFO[name]
    return {"name": name, "bias": p.get("bias"), "status": p.get("status"), "label": label, "tip": tip, "lines": lines,
            "target": p.get("target") if finite(p.get("target")) else None}


def _live_row(live: Any, live_date: Any, keep: int) -> Optional[List[Any]]:
    try:
        if live is None or not isinstance(live_date, str) or len(live_date) < keep or len(live) < 5 or not all(finite(x) for x in live[1:5]):
            return None
        return [live_date[:keep], r2(live[O]), r2(live[H]), r2(live[L]), r2(live[C]), float(live[V]) if len(live) > 5 and finite(live[V]) else 0.0]
    except Exception:  # noqa: BLE001
        return None


def _ago_text(ago: int, intraday: bool) -> str:
    if intraday:
        return "on the latest closed candle" if ago == 0 else ("1 candle ago" if ago == 1 else f"{ago} candles ago")
    return "today" if ago == 0 else ("yesterday" if ago == 1 else f"{ago} days ago")


def chart_context(raw_bars: Any, dates: Any, tail: int = 300, structure_trend: Optional[str] = None, tf: str = "1d", live: Any = None,
                  live_date: Any = None, lag_s: Any = None) -> Dict[str, Any]:
    sp = spec(tf) or spec("1d")
    intraday, u, keep = bool(sp["intraday"]), sp["unit"], (16 if sp["intraday"] else 10)
    out: Dict[str, Any] = {"schema": SCHEMA, "context_only": True, "ok": False, "tf": sp["key"], "unit": u, "note": sp.get("note"), "live": None,
                           "data_lag_s": int(lag_s) if finite(lag_s) else None, "bars": [], "last_bar": None, "markers": [], "shapes": [],
                           "levels": [], "badges": [], "events": [], "regime": {"trend": None, "volatility": None, "momentum": None, "structure": None},
                           "errors": []}
    try:
        bars, ds = _clean(raw_bars, dates, keep)
        n = len(bars)
        if n < 5:
            return out
        tail = max(30, int(tail))
        lo = max(0, n - tail)
        out["bars"] = [[ds[i], r2(bars[i][O]), r2(bars[i][H]), r2(bars[i][L]), r2(bars[i][C]), bars[i][V]] for i in range(lo, n)]
        out["last_bar"] = ds[-1]
        out["live"] = _live_row(live, live_date, keep)
        out["ok"] = True
        pat = analyze_patterns(bars)
        out["errors"].extend(pat.get("errors") or [])
        badges: List[Dict[str, Any]] = []

        # ---- chart patterns -> shapes + badges (confirmed first)
        chart = sorted(pat.get("chart") or [], key=lambda d: (d.get("status") != "confirmed", d.get("name", "")))
        for p in chart[:MAX_SHAPES]:
            try:
                sh = _shape(p, ds, lo, n)
                if sh:
                    out["shapes"].append(sh)
                label, tip = CHART_INFO.get(p.get("name"), (_title(p.get("name", "")), ""))
                st = p.get("status")
                extra = " Confirmed: price closed beyond the line." if st == "confirmed" else " Still forming: not confirmed until price closes beyond the line."
                badges.append({"key": p.get("name"), "kind": "chart", "label": label, "tone": _TONE.get(p.get("bias"), "mute"),
                               "status": st, "tip": tip + extra})
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"shape:{type(e).__name__}")

        # ---- candles on the last 3 bars -> markers + badges (newest first)
        candles = sorted(pat.get("candles") or [], key=lambda d: d.get("bars_ago", 9))
        for c in candles:
            name, ago = c.get("name"), c.get("bars_ago", 0)
            i = n - 1 - ago
            if i < lo or name not in CANDLE_INFO:
                continue
            if intraday and crosses_session(bars, i, span_of(name)):
                continue          # an overnight gap is not part of a 5-minute pattern
            out["markers"].append({"date": ds[i], "name": name, "label": CANDLE_INFO[name][0], "bias": c.get("bias"),
                                   "volume_confirmed": bool(c.get("volume_confirmed")), "bars_ago": ago})
            label, tip = CANDLE_INFO[name]
            when = _ago_text(ago, intraday)
            vol = " Traded on above-average volume." if c.get("volume_confirmed") else ""
            badges.append({"key": name, "kind": "candle", "label": label, "tone": _TONE.get(c.get("bias"), "mute"), "bars_ago": ago,
                           "tip": f"{tip} Seen {when}.{vol}"})

        # ---- signals: S/R flip, squeeze, crosses, MACD + RSI
        for f in (pat.get("sr_flip") or [])[:2]:
            sup = f.get("type") == "support_flip"
            out["levels"].append({"kind": f.get("type"), "price": f.get("level")})
            badges.append({"key": f.get("type"), "kind": "signal", "tone": "good" if sup else "bad",
                           "label": "Old resistance is now support" if sup else "Old support is now resistance",
                           "tip": f"Price broke {f.get('level')} and came back to test it from the other side. It held."})
        bb = pat.get("bollinger")
        if isinstance(bb, dict):
            if bb.get("squeeze"):
                badges.append({"key": "squeeze", "kind": "signal", "label": "Squeeze", "tone": "warn",
                               "tip": f"The Bollinger Bands are among the narrowest of the last 120 {u}s. Price is coiled; a big move often follows, but the direction is not known yet."})
            elif bb.get("release") in ("bullish", "bearish"):
                up = bb["release"] == "bullish"
                badges.append({"key": "squeeze_release", "kind": "signal", "label": "Squeeze broke up" if up else "Squeeze broke down",
                               "tone": "good" if up else "bad", "tip": "A tight squeeze just ended with a close outside the band."})
        cr = pat.get("crosses") or {}
        for key, nm in (("ma50_200", "50/200"), ("ma20_50", "20/50")):
            ev = (cr.get(key) or {}).get("event") if isinstance(cr.get(key), dict) else None
            if ev:
                gold = ev.get("type") == "golden_cross"
                ago = ev.get("bars_ago", 0)
                badges.append({"key": f"{key}_{ev.get('type')}", "kind": "signal", "tone": "good" if gold else "bad", "bars_ago": ago,
                               "label": f"{'Golden' if gold else 'Death'} cross {nm}",
                               "tip": f"The {nm.split('/')[0]}-{u} average crossed {'above' if gold else 'below'} the {nm.split('/')[1]}-{u} average {ago} {u}(s) ago."})
        cf = pat.get("confluence")
        mom = None
        if isinstance(cf, dict) and cf.get("state") in ("bullish", "bearish"):
            up = cf["state"] == "bullish"
            mom = "up" if up else "down"
            badges.append({"key": "macd_rsi", "kind": "signal", "label": "MACD + RSI up" if up else "MACD + RSI down", "tone": "good" if up else "bad",
                           "tip": f"MACD is {'above' if up else 'below'} its signal line with the histogram {'rising' if up else 'falling'}, and RSI is {cf.get('rsi')} ({'50-70' if up else '30-50'} zone)."})
        elif isinstance(cf, dict):
            mom = "flat"

        # ---- regime
        reg = out["regime"]
        tr = _regime_trend(bars, u)
        if tr:
            reg["trend"] = tr["word"]
            badges.append({"key": "regime_trend", "kind": "regime", "label": tr["label"], "tone": tr["tone"], "tip": tr["tip"]})
        if isinstance(bb, dict):
            pct = bb.get("percentile")
            reg["volatility"] = "squeeze" if bb.get("squeeze") else ("wide" if isinstance(pct, (int, float)) and pct >= 90 else "normal")
            if reg["volatility"] == "wide":
                badges.append({"key": "regime_wide", "kind": "regime", "label": "Volatile", "tone": "warn",
                               "tip": f"The Bollinger Bands are among the widest of the last 120 {u}s. Moves are large right now."})
        reg["momentum"] = mom
        if structure_trend in ("bullish", "bearish", "neutral", "transition"):
            reg["structure"] = structure_trend
            word = {"bullish": "up", "bearish": "down", "neutral": "sideways", "transition": "changing"}[structure_trend]
            badges.append({"key": "regime_structure", "kind": "regime", "label": f"{'Daily structure' if intraday else 'Structure'}: {word}",
                           "tone": {"bullish": "good", "bearish": "bad"}.get(structure_trend, "mute"),
                           "tip": "The swing-high / swing-low read of the daily chart (higher highs and higher lows is up)."})
        # regime chips first (always kept), then chart patterns, candles and signals in the order they were built
        regime_first = [b for b in badges if b.get("kind") == "regime"]
        rest = [b for b in badges if b.get("kind") != "regime"]
        out["badges"] = (regime_first + rest)[:MAX_BADGES]
        # E29: the alert-worthy readings (signals and CONFIRMED chart patterns) before the badge cap, so a crowded chart can never hide one from the
        # watchlist digest. Additive field (schema stays 1); the chart itself does not read it.
        out["events"] = [{k: b.get(k) for k in ("key", "kind", "label", "tone", "status", "bars_ago")} for b in badges
                         if b.get("kind") == "signal" or (b.get("kind") == "chart" and b.get("status") == "confirmed")]
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"context:{type(e).__name__}")
    return out
