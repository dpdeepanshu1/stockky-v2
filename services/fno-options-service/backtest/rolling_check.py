"""backtest/rolling_check.py - sanity check of the files scripts/fno_rolling_fetch.py wrote (round 256). Pure (no network).

Why: the fetch was never checked against reality until round 255 (the window limit in the docs was wrong). Its other assumptions are still unproven: that a timestamp is the START of the bar and
sits on the 5-minute grid, that `spot` is the index, that the ATM label follows the spot, that no weekend candle appears, that every trading day has the same number of bars. The backtest trusts all
of them. This reports each one per file and lists `problems` (a result built on a file with a problem should not be trusted); `notes` are facts worth a look that are not errors (days without candles:
holidays, or a hole).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any, Dict, List, Optional

IST = timezone(timedelta(hours=5, minutes=30))
STEP = {"NIFTY": 50.0, "SENSEX": 100.0}
OPEN_MIN = 9 * 60 + 15


def _dt(t: float) -> datetime:
    return datetime.fromtimestamp(t, IST)


def check_doc(doc: Dict[str, Any], holidays: Optional[List[str]] = None) -> Dict[str, Any]:
    rows = [r for r in (doc.get("rows") or []) if r.get("t") is not None]
    und = str(doc.get("underlying") or "NIFTY").upper()
    step = STEP.get(und, 50.0)
    iv = int(doc.get("interval") or 5)
    label = str(doc.get("strike") or "")
    problems: List[str] = []
    notes: List[str] = []
    out: Dict[str, Any] = {"file": f"{und} {doc.get('type')} {label} {doc.get('expiry_flag')}{doc.get('expiry_code')} {iv}m", "rows": len(rows), "problems": problems, "notes": notes}
    if doc.get("complete") is not True:
        problems.append(f"not complete: {len(doc.get('failed_windows') or [])} failed window(s)")
    if not rows:
        problems.append("no candles")
        return out
    per_day: Dict[date, List[float]] = {}
    for r in rows:
        per_day.setdefault(_dt(r["t"]).date(), []).append(r["t"])
    weekend = [d.isoformat() for d in per_day if d.weekday() >= 5]
    if weekend:
        problems.append(f"candles on a weekend: {weekend[:3]}")
    mins = [_dt(r["t"]).hour * 60 + _dt(r["t"]).minute for r in rows]
    off_grid = sum(1 for m, r in zip(mins, rows) if (m - OPEN_MIN) % iv != 0 or _dt(r["t"]).second != 0)
    if off_grid:
        problems.append(f"{off_grid} candle(s) not on the {iv}-minute grid that starts 09:15 (timestamps are not bar starts, or are shifted)")
    before_open = sum(1 for m in mins if m < OPEN_MIN)
    if before_open:
        problems.append(f"{before_open} candle(s) before 09:15")
    out["first_bar"], out["last_bar"] = f"{min(mins) // 60:02d}:{min(mins) % 60:02d}", f"{max(mins) // 60:02d}:{max(mins) % 60:02d}"
    out["after_close_bars"] = sum(1 for m in mins if m >= 15 * 60 + 30)          # 15:30 and later: after the cash close (round 255: Dhan sends 15:30 and 15:35 rows)
    counts = {d: len(v) for d, v in per_day.items()}
    med = median(counts.values())
    out["days"], out["bars_per_day_median"], out["bars_per_day_min"], out["bars_per_day_max"] = len(counts), med, min(counts.values()), max(counts.values())
    thin = sorted(d.isoformat() for d, n in counts.items() if n < 0.8 * med)
    if thin:
        notes.append(f"{len(thin)} day(s) with under 80% of the usual bars (half-day session, or a hole): {thin[:5]}")
    dup = len(rows) - len({r["t"] for r in rows})
    if dup:
        problems.append(f"{dup} duplicate timestamp(s)")
    if [r["t"] for r in rows] != sorted(r["t"] for r in rows):
        problems.append("rows are not in time order")
    try:
        lo, hi = date.fromisoformat(str(doc.get("from"))), date.fromisoformat(str(doc.get("to")))
    except ValueError:
        lo = hi = None
    if lo and hi:
        hol = set(holidays or [])
        d, missing = lo, []
        while d < hi:
            if d.weekday() < 5 and d not in per_day and d.isoformat() not in hol:
                missing.append(d.isoformat())
            d += timedelta(days=1)
        if missing:
            notes.append(f"{len(missing)} weekday(s) in the range have no candles (holiday, or a hole): {missing[:8]}")
        outside = [d.isoformat() for d in per_day if not lo <= d < hi]
        if outside:
            problems.append(f"candles outside the requested range [{lo}, {hi}): {outside[:3]}")
    spots = [(r.get("spot"), r.get("strike")) for r in rows if r.get("spot") and r.get("strike")]
    if spots:
        gaps = [abs(k - s) / step for s, k in spots]
        out["spot_gap_steps_max"], out["spot_gap_steps_mean"] = round(max(gaps), 2), round(sum(gaps) / len(gaps), 2)
        if label == "ATM":
            if max(gaps) > 1.0:
                problems.append(f"the ATM strike is up to {max(gaps):.1f} steps from spot (expected within 0.5 step): is `spot` the index, is the label ATM?")
            if len({k for _, k in spots}) < 2 and len(per_day) > 1:
                problems.append("the ATM strike never changes: the label does not follow the spot")
        out["distinct_strikes"] = len({k for _, k in spots})
    else:
        notes.append("no spot / strike columns in the rows (requiredData was not honoured?)")
    out["zero_or_missing_iv"] = sum(1 for r in rows if not r.get("iv"))
    out["zero_or_missing_oi"] = sum(1 for r in rows if not r.get("oi"))
    out["missing_volume"] = sum(1 for r in rows if r.get("v") is None)
    return out


def format_report(rep: Dict[str, Any]) -> List[str]:
    head = f"{rep['file']}: {rep['rows']} candles"
    if rep["rows"]:
        head += (f", {rep['days']} days, {rep['bars_per_day_min']}-{rep['bars_per_day_max']} bars/day (median {rep['bars_per_day_median']}), bars {rep['first_bar']}..{rep['last_bar']} "
                 f"({rep['after_close_bars']} at/after 15:30)")
        if "spot_gap_steps_max" in rep:
            head += f", strike-spot gap max {rep['spot_gap_steps_max']} / mean {rep['spot_gap_steps_mean']} steps, {rep['distinct_strikes']} distinct strikes"
        head += f", iv missing {rep['zero_or_missing_iv']}, oi missing {rep['zero_or_missing_oi']}"
    lines = [head]
    lines += [f"   PROBLEM: {p}" for p in rep["problems"]]
    lines += [f"   note: {n}" for n in rep["notes"]]
    return lines
