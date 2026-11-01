#!/usr/bin/env python3
"""sweep_study.py - E278: after a liquidity sweep, does price reverse or continue on NSE stocks? Offline only, never touches live trading.

  python3 scripts/sweep_study.py [--daily HIST_DIR] [--bars BAR_DIR] [--out DIR] [--parts A,B,C]
                                 [--depths 0.10[,0.20,...]] [--horizons 1,3,5] [--cost-pct 0.30] [--slip-pct 0.10]
                                 [--ic-cost-pct 0.15] [--boot 500] [--min-events 30] [--sample 0 --seed 7]

HIST_DIR = the daily-history folder scripts/fetch_history_for_backtest.py writes (same loader as scripts/ma_baseline.py: <SYMBOL>.json with "candles",
           a benchmark file such as NIFTY.json / ^NSEI.json is used for the market-adjusted view).
BAR_DIR  = the 1-minute folders written by position-stocks feed/bar_recorder.py (compose volume `bar-records`, copied to the host).

Direction-agnostic: EVERY event is scored as a long and as a short, so the answer is reversal, continuation or neither - not "does the video work".

  A  Daily (video step 1). Event day i of a stock:
       sweep_low    low pierced the previous low by >= DEPTH x ATR14(i-1) and the day CLOSED back above that low   (video: "ab sirf buy")
       sweep_high   mirror
       break_high   close BEYOND the previous high  (the second, continuation-looking ICT bias rule)
       break_low    mirror
     A day that pierced both sides (an outside bar) is skipped and counted. Entry = OPEN of day i+1; exit = close of day i+h (h=1 is the next
     session open to close; 3 and 5 are reported too). Net = signed gross - (cost + slippage), both directions pay the same friction.
     Baseline = the same stock's NON-event days of the SAME MONTH, same horizon (needs >= 5 such days, else the event is dropped and counted).
     Two readings are produced from the same events: raw, and market-adjusted (the benchmark's return over the same window is subtracted).
  B  30-minute sweep + 5-minute push (video steps 2 and 3) on recorded bars. COUNTS ONLY: how many full 3-step patterns a day. The verdict is
     insufficient_sample under --min-events and counts_only above it; it is never an edge verdict. (Bias uses the recorded days' own range as the ATR proxy:
     there are too few recorded days for ATR14.)
  C  Failed opening-range break (the "manipulation" picture). Range = the 09:15-09:19 one-minute bars. First closed 1-minute break before 09:55;
     "failed" = a close back inside the range within 3 bars (entry = next bar open), "held" = no such close (entry = the open 4 bars after the break, so
     both groups enter at a comparable time). Returns to 10:00, 10:30 and 15:00, cost --ic-cost-pct. Baseline = the held breaks, scored the same way.

PRE-REGISTERED (to avoid fishing): ONE sweep depth (the first of --depths, default 0.10 ATR = the engine's sweep_min_atr), ONE displacement size (1.5 x ATR,
60 % body = the engine's order-block rule), ONE reclaim window (3 bars), ONE primary horizon (h=1 for A; 10:00 for C). Extra depths / horizons are reported
but the verdict uses only the first of each. Every variant tried is counted in the report ("variants_tried").

VERDICT per event kind (primary horizon):
  insufficient_sample  fewer than --min-events events (or fewer than 20 day clusters): ONLY counts are printed, nothing else.
  reversal             the reversal side beats its baseline AND is positive after costs: the 90 % day-clustered bootstrap interval of BOTH is above zero.
  continuation         the same for the continuation side.
  neither              anything else (this is the expected answer for most kinds).
Day-clustered because every stock of a day shares the market's move. Past data are not a promise; this decides whether a rule is worth building (E279-E281), nothing more.
Output: markdown on stdout; with --out also sweep_study.json and sweep_study.md. Stdlib only.
"""
from __future__ import annotations

import argparse
import bisect
import json
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _intraday_data as idata  # noqa: E402
import ms_ab_report as ab  # noqa: E402
from market_structure.atr import atr_series  # noqa: E402
from market_structure.candles import day_key, ist_date  # noqa: E402
from market_structure.ms_types import C, H, L, O, T, finite  # noqa: E402

MIN_EVENTS = 30            # fewer events per direction than this -> insufficient_sample and nothing else
MIN_CLUSTERS = 20          # fewer day clusters than this -> no interval, so no verdict
MIN_BASELINE_DAYS = 5      # matched non-event days of the same stock and month
PRIMARY_DEPTH = 0.10       # x ATR14: the engine's sweep_min_atr
DISP_ATR = 1.5             # displacement body >= 1.5 x ATR (the order-block rule)
DISP_BODY = 0.60           # ... and >= 60 % of the bar's range
RECLAIM_BARS = 3
KINDS = ("sweep_low", "sweep_high", "break_high", "break_low")
# for each kind, the side (+1 long / -1 short) that would be the REVERSAL trade; the other side is the continuation trade
REVERSAL_SIDE = {"sweep_low": 1, "sweep_high": -1, "break_high": -1, "break_low": 1}
JUMP_PCT = 20.0            # skip a day whose close moved more than this vs the previous close (split / bonus artefact), or whose next open gaps this much


# ----------------------------------------------------------------------------------------------------------------------------------------
# shared statistics
# ----------------------------------------------------------------------------------------------------------------------------------------
def boot(clusters: Sequence[Sequence[float]], n_boot: int, seed: int = 7) -> Dict[str, Any]:
    """Cluster bootstrap. Each cluster = [na, sum_net_a, nb, sum_net_b] (a day's events and its baseline). Returns 90 % intervals for mean(a) (after costs)
    and for mean(a) - mean(b) (against the baseline). None when there are fewer than MIN_CLUSTERS clusters."""
    out: Dict[str, Any] = {"clusters": len(clusters), "ci_net": None, "ci_excess": None, "p_net_positive": None}
    if len(clusters) < MIN_CLUSTERS or n_boot < 50:
        return out
    rng = random.Random(seed)
    m = len(clusters)
    nets: List[float] = []
    exc: List[float] = []
    for _ in range(n_boot):
        na = sa = nb = sb = 0.0
        for k in rng.choices(range(m), k=m):
            c = clusters[k]
            na += c[0]
            sa += c[1]
            nb += c[2]
            sb += c[3]
        if na > 0:
            nets.append(sa / na)
            if nb > 0:
                exc.append(sa / na - sb / nb)
    if len(nets) < n_boot // 2 or len(exc) < n_boot // 2:
        return out
    nets.sort()
    exc.sort()
    q = lambda v, p: round(v[int(p * (len(v) - 1))], 3)  # noqa: E731
    out["ci_net"] = [q(nets, 0.05), q(nets, 0.95)]
    out["ci_excess"] = [q(exc, 0.05), q(exc, 0.95)]
    out["p_net_positive"] = round(sum(1 for x in nets if x > 0) / len(nets), 3)
    return out


def side_ok(b: Dict[str, Any]) -> bool:
    """One side is an edge only when BOTH 90 % intervals (net after costs, and excess over the baseline) sit above zero."""
    return bool(b.get("ci_net") and b.get("ci_excess") and b["ci_net"][0] > 0 and b["ci_excess"][0] > 0)


def verdict(n_events: int, rev: Dict[str, Any], cont: Dict[str, Any], min_events: int = MIN_EVENTS) -> str:
    if n_events < min_events or rev.get("ci_net") is None or cont.get("ci_net") is None:
        return "insufficient_sample"
    r, c = side_ok(rev), side_ok(cont)
    if r and not c:
        return "reversal"
    if c and not r:
        return "continuation"
    return "neither"


def _mean(v: Sequence[float]) -> Optional[float]:
    return round(sum(v) / len(v), 3) if v else None


# ----------------------------------------------------------------------------------------------------------------------------------------
# PART A - daily sweeps
# ----------------------------------------------------------------------------------------------------------------------------------------
def classify_day(bars: Sequence[Sequence[float]], atr: Sequence[Optional[float]], i: int, depth: float) -> Optional[str]:
    """The event kind of day i (known at its close), or None. Uses the previous bar's high / low and ATR14(i-1). An outside bar (both sides pierced) is 'outside'."""
    if i < 1:
        return None
    a = atr[i - 1]
    if not a or a <= 0:
        return None
    pb, b = bars[i - 1], bars[i]
    if not all(finite(x) for x in (pb[H], pb[L], b[H], b[L], b[C], pb[C])) or pb[C] <= 0:
        return None
    if abs(b[C] / pb[C] - 1.0) * 100.0 > JUMP_PCT:
        return None
    thr = depth * a
    hi_pierce = b[H] - pb[H] >= thr and b[H] > pb[H]
    lo_pierce = pb[L] - b[L] >= thr and b[L] < pb[L]
    if hi_pierce and lo_pierce:
        return "outside"
    if lo_pierce and b[C] > pb[L]:
        return "sweep_low"
    if hi_pierce and b[C] < pb[H]:
        return "sweep_high"
    if b[C] > pb[H]:
        return "break_high"
    if b[C] < pb[L]:
        return "break_low"
    return None


def gross_ret(bars: Sequence[Sequence[float]], i: int, h: int) -> Optional[float]:
    """Long return % of: buy the OPEN of day i+1, sell the CLOSE of day i+h (h=1: the same session). None when the bars end first or the open gaps absurdly."""
    j = i + h
    if i + 1 >= len(bars) or j >= len(bars):
        return None
    op, cl, pc = bars[i + 1][O], bars[j][C], bars[i][C]
    if not (finite(op) and finite(cl) and op > 0 and cl > 0 and pc > 0):
        return None
    if abs(op / pc - 1.0) * 100.0 > JUMP_PCT:
        return None
    return (cl / op - 1.0) * 100.0


def bench_ret(bench: Optional[Dict[str, Any]], bars: Sequence[Sequence[float]], i: int, h: int) -> Optional[float]:
    """The benchmark's return over the same window (open of day i+1 -> close of day i+h), matched by calendar day. None without a benchmark or a matching day."""
    if bench is None or i + 1 >= len(bars) or i + h >= len(bars):
        return None
    k0 = bisect.bisect_left(bench["keys"], day_key(bars[i + 1][T]))
    k1 = bisect.bisect_left(bench["keys"], day_key(bars[i + h][T]))
    if k0 >= len(bench["keys"]) or k1 >= len(bench["keys"]):
        return None
    if bench["keys"][k0] != day_key(bars[i + 1][T]) or bench["keys"][k1] != day_key(bars[i + h][T]):
        return None
    o, c = bench["open"][k0], bench["close"][k1]
    return (c / o - 1.0) * 100.0 if o and c else None


def stock_events(bars: Sequence[Sequence[float]], sym: str, depth: float, horizons: Sequence[int], bench: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Events and matched baselines for one symbol. A row holds, per horizon h, the LONG gross return g{h}, its matched baseline b{h} and the market return m{h}."""
    n = len(bars)
    out: Dict[str, Any] = {"events": [], "outside": 0, "no_baseline": 0, "scanned": 0}
    if n < 40:
        return out
    atr = atr_series(bars, 14)
    kind = [classify_day(bars, atr, i, depth) for i in range(n)]
    out["outside"] = sum(1 for k in kind if k == "outside")
    month = [ist_date(b[T])[:7] for b in bars]
    # per month: gross returns of the NON-event days, per horizon
    pool: Dict[Tuple[str, int], List[float]] = defaultdict(list)
    for i in range(n - 1):
        if kind[i] is not None:                       # an event or an outside bar is never part of the baseline
            continue
        for h in horizons:
            g = gross_ret(bars, i, h)
            if g is not None:
                pool[(month[i], h)].append(g)
    for i in range(n - 1):
        k = kind[i]
        if k not in KINDS:
            continue
        out["scanned"] += 1
        row: Dict[str, Any] = {"sym": sym, "date": ist_date(bars[i][T]), "kind": k}
        have = False
        for h in horizons:
            g = gross_ret(bars, i, h)
            base = pool.get((month[i], h), [])
            if g is None or len(base) < MIN_BASELINE_DAYS:
                row[f"g{h}"] = row[f"b{h}"] = row[f"m{h}"] = None
                continue
            row[f"g{h}"] = g
            row[f"b{h}"] = sum(base) / len(base)
            row[f"m{h}"] = bench_ret(bench, bars, i, h)
            have = True
        if have:
            out["events"].append(row)
        else:
            out["no_baseline"] += 1
    return out


def _clusters_a(rows: Sequence[Dict[str, Any]], h: int, side: int, friction: float, adjust: bool) -> List[List[float]]:
    by_day: Dict[str, List[float]] = {}
    for r in rows:
        g, b = r.get(f"g{h}"), r.get(f"b{h}")
        if g is None or b is None:
            continue
        m = 0.0
        if adjust:
            m = r.get(f"m{h}")
            if m is None:
                continue
            # the baseline is a plain stock return too: take the same market return off it so only the sweep's own effect is left
        c = by_day.setdefault(r["date"], [0.0, 0.0, 0.0, 0.0])
        c[0] += 1
        c[1] += side * (g - m) - friction
        c[2] += 1
        c[3] += side * (b - m) - friction
    return list(by_day.values())


def summarize_a(rows: Sequence[Dict[str, Any]], kind: str, h: int, friction: float, n_boot: int, adjust: bool, min_events: int) -> Dict[str, Any]:
    sel = [r for r in rows if r["kind"] == kind and r.get(f"g{h}") is not None and (not adjust or r.get(f"m{h}") is not None)]
    days = len({r["date"] for r in sel})
    res: Dict[str, Any] = {"kind": kind, "horizon": h, "n": len(sel), "days": days}
    if len(sel) < min_events:
        res["verdict"] = "insufficient_sample"
        return res
    sides: Dict[str, Dict[str, Any]] = {}
    for name, s in (("long", 1), ("short", -1)):
        cl = _clusters_a(sel, h, s, friction, adjust)
        b = boot(cl, n_boot)
        na = sum(c[0] for c in cl)
        b["mean_net"] = round(sum(c[1] for c in cl) / na, 3) if na else None
        b["mean_excess"] = round(sum(c[1] - c[3] for c in cl) / na, 3) if na else None
        sides[name] = b
    rev_name = "long" if REVERSAL_SIDE[kind] > 0 else "short"
    cont_name = "short" if rev_name == "long" else "long"
    res.update({"long": sides["long"], "short": sides["short"], "reversal_side": rev_name, "continuation_side": cont_name,
                "verdict": verdict(len(sel), sides[rev_name], sides[cont_name], min_events)})
    return res


def run_part_a(hist_dir: Path, depths: Sequence[float], horizons: Sequence[int], cost: float, slip: float, n_boot: int, min_events: int,
               sample: int, seed: int) -> Dict[str, Any]:
    files = sorted(hist_dir.glob("*.json"))
    bench = None
    for fl in files:
        if fl.stem.upper() in ab.BENCH_NAMES:
            try:
                bb = ab.load_bars(fl)
                if len(bb) >= 220:
                    bench = {"keys": [day_key(b[T]) for b in bb], "open": [b[O] for b in bb], "close": [b[C] for b in bb]}
            except Exception as e:  # noqa: BLE001
                print(f"benchmark {fl.name} unreadable: {e}", file=sys.stderr)
            break
    stock_files = [fl for fl in files if fl.stem.upper() not in ab.BENCH_NAMES]
    if sample > 0 and len(stock_files) > sample:
        stock_files = sorted(random.Random(seed).sample(stock_files, sample))
    loaded: List[Tuple[str, List[Tuple[float, ...]]]] = []
    skipped = 0
    for fl in stock_files:
        try:
            loaded.append((fl.stem.upper(), ab.load_bars(fl)))
        except Exception as e:  # noqa: BLE001
            skipped += 1
            print(f"skip {fl.name}: {e}", file=sys.stderr)
    friction = cost + slip
    variants: List[Dict[str, Any]] = []
    for d_idx, depth in enumerate(depths):
        rows: List[Dict[str, Any]] = []
        outside = no_base = 0
        for sym, bars in loaded:
            r = stock_events(bars, sym, depth, horizons, bench)
            rows += r["events"]
            outside += r["outside"]
            no_base += r["no_baseline"]
        tables = []
        for adjust in ((False, True) if bench is not None else (False,)):
            for kind in KINDS:
                for h in horizons:
                    t = summarize_a(rows, kind, h, friction, n_boot, adjust, min_events)
                    t["market_adjusted"] = adjust
                    tables.append(t)
        variants.append({"depth_atr": depth, "primary": d_idx == 0, "events": len(rows), "outside_bars_skipped": outside, "events_without_baseline": no_base, "tables": tables})
    return {"symbols": len(loaded), "skipped": skipped, "benchmark": bench is not None, "friction_pct": round(friction, 3), "horizons": list(horizons),
            "primary_horizon": horizons[0], "variants": variants}


# ----------------------------------------------------------------------------------------------------------------------------------------
# PART B - 30-minute sweep + 5-minute push: counts only
# ----------------------------------------------------------------------------------------------------------------------------------------
def _agg(bars: Sequence[Sequence[float]], t0: float, width: int, min_bars: int) -> Dict[int, Tuple[float, float, float, float]]:
    """{bucket: (open, high, low, close)} of the 1-minute bars grouped into `width`-minute buckets counted from 09:15; a bucket with fewer than min_bars is dropped."""
    g: Dict[int, List[Sequence[float]]] = defaultdict(list)
    for b in bars:
        off = int(round((b[0] - t0) / 60.0))
        if 0 <= off < 375:
            g[off // width].append(b)
    out: Dict[int, Tuple[float, float, float, float]] = {}
    for k, v in g.items():
        if len(v) < min_bars:
            continue
        v.sort(key=lambda x: x[0])
        out[k] = (v[0][1], max(x[2] for x in v), min(x[3] for x in v), v[-1][4])
    return out


def daily_bias(prev: Tuple[float, float, float, float], prev2: Tuple[float, float, float, float], ref_range: float) -> int:
    """+1 / -1 / 0. prev, prev2 = (o, h, l, c) of the last two recorded days. Bullish: prev pierced prev2's low by >= PRIMARY_DEPTH x ref_range and closed back above it."""
    thr = PRIMARY_DEPTH * ref_range
    lo = prev2[2] - prev[2] >= thr and prev[2] < prev2[2] and prev[3] > prev2[2]
    hi = prev[1] - prev2[1] >= thr and prev[1] > prev2[1] and prev[3] < prev2[1]
    return 1 if lo and not hi else (-1 if hi and not lo else 0)


def three_step_events(bars: Sequence[Sequence[float]], date: str, bias: int) -> List[Dict[str, Any]]:
    """The full video pattern on one recorded day, given the daily bias: a 30-minute candle wicks through the previous 30-minute candle's far end on the bias
    side and closes back inside (rejection: close in the outer half of its range), then within RECLAIM_BARS five-minute bars after it completes a displacement
    candle in the bias direction closes back inside the range, with an opposite-colour candle (the order block) within 5 bars before it."""
    if bias == 0:
        return []
    t0 = idata.open_ts(date)
    c30 = _agg(bars, t0, 30, 25)
    c5 = _agg(bars, t0, 5, 4)
    if len(c5) < 12:
        return []
    ks = sorted(c5)
    # ATR of the five-minute bars: mean true range of the previous <= 14 bars (needs >= 6)
    tr: Dict[int, float] = {}
    prev_c: Optional[float] = None
    for k in ks:
        o, h, l_, c = c5[k]
        tr[k] = max(h - l_, abs(h - prev_c), abs(l_ - prev_c)) if prev_c is not None else h - l_
        prev_c = c
    found: List[Dict[str, Any]] = []
    for k in sorted(c30):
        if k < 1 or (k - 1) not in c30:
            continue
        po, ph, pl, pc = c30[k - 1]
        o, h, l_, c = c30[k]
        rng = h - l_
        if rng <= 0:
            continue
        end_idx = 6 * (k + 1)                                  # first 5-minute bucket after this 30-minute candle completes
        prior = [x for x in ks if x < end_idx]
        if len(prior) < 7:
            continue
        atr5 = sum(tr[x] for x in prior[-14:]) / len(prior[-14:])
        thr = PRIMARY_DEPTH * atr5
        if bias > 0:
            ok = pl - l_ >= thr and l_ < pl and pl < c < ph and (c - l_) / rng >= 0.5
            level = pl
        else:
            ok = h - ph >= thr and h > ph and pl < c < ph and (h - c) / rng >= 0.5
            level = ph
        if not ok:
            continue
        for j in range(end_idx, end_idx + RECLAIM_BARS):
            if j not in c5:
                continue
            jo, jh, jl, jc = c5[j]
            body, jr = abs(jc - jo), jh - jl
            atr_j = [tr[x] for x in ks if x < j][-14:]
            if len(atr_j) < 6 or jr <= 0:
                continue
            a = sum(atr_j) / len(atr_j)
            if body < DISP_ATR * a or body < DISP_BODY * jr:
                continue
            if (bias > 0 and not (jc > jo and jc > level)) or (bias < 0 and not (jc < jo and jc < level)):
                continue
            ob = None
            for q in range(j - 1, max(min(ks) - 1, j - 6), -1):
                if q in c5 and ((bias > 0 and c5[q][3] < c5[q][0]) or (bias < 0 and c5[q][3] > c5[q][0])):
                    ob = q
                    break
            if ob is None:
                continue
            found.append({"candle30": k, "push5": j, "ob5": ob, "bias": bias, "setup_complete_min": 30 * (k + 1), "date": date})
            break
    return found


def run_part_b(bar_dir: Path, min_events: int) -> Dict[str, Any]:
    st: Dict[str, int] = {}
    data = idata.load_dir(str(bar_dir), stats=st)
    events: List[Dict[str, Any]] = []
    sym_days = bias_days = 0
    dates = set()
    for sym, days in data.items():
        done = sorted(d for d, v in days.items() if v["complete"])
        daily = {}
        for d in done:
            bs = days[d]["bars"]
            daily[d] = (bs[0][1], max(b[2] for b in bs), min(b[3] for b in bs), bs[-1][4])
        for idx in range(2, len(done)):
            d = done[idx]
            if idata.previous_day(days, d) != done[idx - 1] or idata.previous_day(days, done[idx - 1]) != done[idx - 2]:
                continue
            ref = [daily[x][1] - daily[x][2] for x in done[:idx]]
            ref_range = sum(ref) / len(ref) if ref else 0.0
            if ref_range <= 0:
                continue
            sym_days += 1
            dates.add(d)
            b = daily_bias(daily[done[idx - 1]], daily[done[idx - 2]], ref_range)
            if b == 0:
                continue
            bias_days += 1
            for e in three_step_events(days[d]["bars"], d, b):
                e["sym"] = sym
                events.append(e)
    n_days = len(dates)
    inv = idata.inventory(str(bar_dir), data, st)
    note_b = inv["reason"] or ("" if sym_days else "complete days exist, but no symbol has three recorded complete days in a row (part B needs the two days before, for the daily bias)")
    per_dir = {"long": sum(1 for e in events if e["bias"] > 0), "short": sum(1 for e in events if e["bias"] < 0)}
    ok = all(v >= min_events for v in per_dir.values())
    return {"files": st.get("files", 0), "bad_files": st.get("bad_files", 0), "symbol_days_scanned": sym_days, "symbol_days_with_bias": bias_days,
            "trading_days": n_days, "events": len(events), "events_by_side": per_dir, "events_per_day": round(len(events) / n_days, 2) if n_days else None,
            "days_with_an_event": len({e["date"] for e in events}),
            "verdict": "counts_only" if ok else "insufficient_sample", "data": inv, "why_empty": note_b,
            "note": "counts only: this says whether a 3-step rule (S5) would fire often enough to matter, never whether it pays"}


# ----------------------------------------------------------------------------------------------------------------------------------------
# PART C - failed opening-range break
# ----------------------------------------------------------------------------------------------------------------------------------------
IC_HORIZONS: Dict[str, int] = {"10:00": 45, "10:30": 75, "15:00": 345}     # minutes after 09:15, as in preopen_momentum_study
IC_LAST_BREAK_OFF = 39                                                       # a break before 09:55 so an entry 4 bars later is still before 10:00


def _px_at(mb: Dict[int, tuple], off: int) -> Optional[float]:
    b = mb.get(off)
    if b is not None and b[1] > 0:
        return b[1]
    for k in range(off - 1, off - 6, -1):
        b = mb.get(k)
        if b is not None and b[4] > 0:
            return b[4]
    return None


def opening_break(mb: Dict[int, tuple]) -> Optional[Dict[str, Any]]:
    """The first closed 1-minute break of the 09:15-09:19 range and what happened next: kind 'failed' (a close back inside within 3 bars) or 'held'.
    Returns {kind, side (+1 up-break), break_off, entry_off} or None (no range, no break before 09:55, or a bar needed is missing)."""
    rng = [mb.get(k) for k in range(5)]
    if any(b is None for b in rng):
        return None
    hi5, lo5 = max(b[2] for b in rng), min(b[3] for b in rng)
    if hi5 <= lo5:
        return None
    for off in range(5, IC_LAST_BREAK_OFF + 1):
        b = mb.get(off)
        if b is None:
            continue
        side = 1 if b[4] > hi5 else (-1 if b[4] < lo5 else 0)
        if not side:
            continue
        for k in range(off + 1, off + 1 + RECLAIM_BARS):
            nb = mb.get(k)
            if nb is not None and lo5 <= nb[4] <= hi5:
                return {"kind": "failed", "side": side, "break_off": off, "entry_off": k + 1}
        return {"kind": "held", "side": side, "break_off": off, "entry_off": off + RECLAIM_BARS + 1}
    return None


def ic_observations(data: Dict[str, Dict[str, Any]], cost: float) -> List[Dict[str, Any]]:
    """One row per (symbol, day) with an opening break; per horizon the WITH-BREAK gross return %, so follow = +gw - cost and fade = -gw - cost."""
    rows: List[Dict[str, Any]] = []
    for sym, days in data.items():
        for date, day in days.items():
            if not day["complete"]:
                continue
            t0 = idata.open_ts(date)
            mb = {int(round((b[0] - t0) / 60.0)): b for b in day["bars"] if 0 <= round((b[0] - t0) / 60.0) < 375}
            ev = opening_break(mb)
            if ev is None:
                continue
            e_px = _px_at(mb, ev["entry_off"])
            if e_px is None:
                continue
            row = {"sym": sym, "date": date, "kind": ev["kind"], "side": ev["side"]}
            for lab, off in IC_HORIZONS.items():
                x = _px_at(mb, off)
                row[lab] = ev["side"] * (x / e_px - 1.0) * 100.0 if x is not None and off > ev["entry_off"] else None
            rows.append(row)
    return rows


def summarize_c(rows: Sequence[Dict[str, Any]], horizon: str, cost: float, n_boot: int, min_events: int) -> Dict[str, Any]:
    failed = [r for r in rows if r["kind"] == "failed" and r.get(horizon) is not None]
    held = [r for r in rows if r["kind"] == "held" and r.get(horizon) is not None]
    res: Dict[str, Any] = {"horizon": horizon, "failed_n": len(failed), "held_n": len(held), "failed_days": len({r["date"] for r in failed})}
    if len(failed) < min_events or len(held) < min_events:
        res["verdict"] = "insufficient_sample"
        return res
    sides: Dict[str, Dict[str, Any]] = {}
    for name, s in (("fade", -1), ("follow", 1)):
        by: Dict[str, List[float]] = {}
        for r in failed:
            c = by.setdefault(r["date"], [0.0, 0.0, 0.0, 0.0])
            c[0] += 1
            c[1] += s * r[horizon] - cost
        for r in held:
            c = by.setdefault(r["date"], [0.0, 0.0, 0.0, 0.0])
            c[2] += 1
            c[3] += s * r[horizon] - cost
        cl = [c for c in by.values() if c[0] > 0 or c[2] > 0]
        b = boot(cl, n_boot)
        na, nb = sum(c[0] for c in cl), sum(c[2] for c in cl)
        b["mean_net_failed"] = round(sum(c[1] for c in cl) / na, 3) if na else None
        b["mean_net_held"] = round(sum(c[3] for c in cl) / nb, 3) if nb else None
        sides[name] = b
    res.update({"fade": sides["fade"], "follow": sides["follow"],
                "verdict": verdict(len(failed), {"ci_net": sides["fade"]["ci_net"], "ci_excess": sides["fade"]["ci_excess"]},
                                   {"ci_net": sides["follow"]["ci_net"], "ci_excess": sides["follow"]["ci_excess"]}, min_events)})
    # for C the names are fade = reversal, follow = continuation
    res["verdict"] = {"reversal": "reversal (fade the failed break)", "continuation": "continuation (follow the failed break)"}.get(res["verdict"], res["verdict"])
    return res


def run_part_c(bar_dir: Path, cost: float, n_boot: int, min_events: int) -> Dict[str, Any]:
    st: Dict[str, int] = {}
    data = idata.load_dir(str(bar_dir), stats=st)
    rows = ic_observations(data, cost)
    days = len({r["date"] for r in rows})
    inv = idata.inventory(str(bar_dir), data, st)
    why = inv["reason"] or ("" if rows else "complete days were read, but no stock has all five 09:15-09:19 bars and a close outside that range before 09:55")
    return {"files": st.get("files", 0), "bad_files": st.get("bad_files", 0), "data": inv, "why_empty": why, "break_days": len(rows), "days": days, "cost_pct": cost,
            "failed": sum(1 for r in rows if r["kind"] == "failed"), "held": sum(1 for r in rows if r["kind"] == "held"),
            "primary_horizon": "10:00", "tables": [summarize_c(rows, h, cost, n_boot, min_events) for h in IC_HORIZONS]}


# ----------------------------------------------------------------------------------------------------------------------------------------
# report
# ----------------------------------------------------------------------------------------------------------------------------------------
def _fmt(v: Any) -> str:
    return "-" if v is None else str(v)


def _data_line(inv: Optional[Dict[str, Any]]) -> str:
    """One line of what the loader read from BAR_DIR (E284): without it a zero says nothing about WHY."""
    if not inv:
        return "- data read: unknown"
    days = ", ".join(f"{d} {c['complete']}/{c['files']}" for d, c in list(inv.get("per_day", {}).items())[-12:]) or "none"
    return (f"- data read from {inv.get('path')}: {inv.get('files', 0)} file(s) ({inv.get('bad_files', 0)} unreadable), {inv.get('symbols', 0)} symbol(s), {inv.get('days_found', 0)} day(s), "
            f"{inv.get('days_complete', 0)} with a complete file; complete/files per day: {days}")


def to_markdown(rep: Dict[str, Any]) -> str:
    L_: List[str] = ["# Sweep study (E278): reversal, continuation or neither", "",
                     "Every event is scored as a long and as a short. `insufficient_sample` means ONLY counts are shown and nothing may be concluded.",
                     f"Variants tried (counted, only the first of each is used for a verdict): {rep['variants_tried']}", ""]
    for part, why in (rep.get("skipped") or {}).items():
        L_ += [f"> Part {part} did NOT run: {why}", ""]
    a = rep.get("A")
    if a:
        L_ += [f"## A. Daily sweeps ({a['symbols']} symbols, skipped {a['skipped']}, friction {a['friction_pct']}% per round trip, benchmark: {'yes' if a['benchmark'] else 'no'})", ""]
        for v in a["variants"]:
            tag = "PRIMARY (verdict)" if v["primary"] else "reported only"
            L_ += [f"### depth {v['depth_atr']} ATR - {tag}: {v['events']} events, {v['outside_bars_skipped']} outside bars skipped, {v['events_without_baseline']} events without a baseline", "",
                   "| kind | h | adj | n | days | reversal side | net % (rev) | CI90 net (rev) | excess % (rev) | CI90 excess (rev) | net % (cont) | CI90 net (cont) | verdict |",
                   "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
            for t in v["tables"]:
                if t.get("verdict") == "insufficient_sample" or "long" not in t:
                    L_.append(f"| {t['kind']} | {t['horizon']} | {'yes' if t['market_adjusted'] else 'no'} | {t['n']} | {t['days']} | - | - | - | - | - | - | - | insufficient_sample |")
                    continue
                r, c = t[t["reversal_side"]], t[t["continuation_side"]]
                L_.append(f"| {t['kind']} | {t['horizon']} | {'yes' if t['market_adjusted'] else 'no'} | {t['n']} | {t['days']} | {t['reversal_side']} | {_fmt(r['mean_net'])} | {_fmt(r['ci_net'])} | "
                          f"{_fmt(r['mean_excess'])} | {_fmt(r['ci_excess'])} | {_fmt(c['mean_net'])} | {_fmt(c['ci_net'])} | {t['verdict']} |")
            L_.append("")
    b = rep.get("B")
    if b:
        L_ += ["## B. 30-minute sweep + 5-minute push (counts only)", "", _data_line(b.get("data")),
               f"- verdict: `{b['verdict']}` ({b['note']})", f"- symbol-days scanned {b['symbol_days_scanned']}, with a daily bias {b['symbol_days_with_bias']}, over {b['trading_days']} recorded trading days" + (f" - WHY EMPTY: {b['why_empty']}" if b.get("why_empty") else ""),
               f"- full 3-step events: {b['events']} (long {b['events_by_side']['long']}, short {b['events_by_side']['short']}); {_fmt(b['events_per_day'])} a day; days with at least one: {b['days_with_an_event']}", ""]
    c = rep.get("C")
    if c:
        L_ += ["## C. Failed opening-range break", "", _data_line(c.get("data")),
               f"- break days {c['break_days']} over {c['days']} days: failed {c['failed']}, held {c['held']}; cost {c['cost_pct']}% round trip; primary horizon {c['primary_horizon']}" + (f" - WHY EMPTY: {c['why_empty']}" if c.get("why_empty") else ""), "",
               "| horizon | failed n | held n | net % fade (failed) | CI90 net fade | CI90 excess fade | net % follow (failed) | verdict |", "|---|---|---|---|---|---|---|---|"]
        for t in c["tables"]:
            if "fade" not in t:
                L_.append(f"| {t['horizon']} | {t['failed_n']} | {t['held_n']} | - | - | - | - | insufficient_sample |")
                continue
            L_.append(f"| {t['horizon']} | {t['failed_n']} | {t['held_n']} | {_fmt(t['fade']['mean_net_failed'])} | {_fmt(t['fade']['ci_net'])} | {_fmt(t['fade']['ci_excess'])} | "
                      f"{_fmt(t['follow']['mean_net_failed'])} | {t['verdict']} |")
        L_.append("")
    L_ += ["## What ships (plan section 4)", "",
           "| study says | ships |", "|---|---|",
           "| reversal after sweeps beats baseline (both 90% intervals above zero) | E279, then E280 if part B shows enough events |",
           "| continuation beats baseline | flip: a fresh sweep is a penalty against reversal entries and a confirmation for continuation; no reversal S5 |",
           "| neither (the likely answer with ~9 days of intraday bars) | nothing sweep-based goes live; sweep fields stay in the stamp as data; part A decides on its own |", "",
           "Caveats: forex-style ICT claims are untested on NSE stocks; part B and C rest on the recorded days only; one sweep depth, one displacement size and one reclaim window are pre-registered; "
           "bar-level approximation, no spread beyond the cost; a verdict is a reason to build or not build, not a promise."]
    return "\n".join(L_) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--daily", default="", help="HIST_DIR for part A")
    ap.add_argument("--bars", default="", help="BAR_DIR for parts B and C")
    ap.add_argument("--out", default="")
    ap.add_argument("--parts", default="A,B,C")
    ap.add_argument("--depths", default=str(PRIMARY_DEPTH), help="comma list; the FIRST is the pre-registered one")
    ap.add_argument("--horizons", default="1,3,5", help="comma list; the FIRST is the primary horizon of part A")
    ap.add_argument("--cost-pct", type=float, default=0.30)
    ap.add_argument("--slip-pct", type=float, default=0.10)
    ap.add_argument("--ic-cost-pct", type=float, default=0.15)
    ap.add_argument("--boot", type=int, default=500)
    ap.add_argument("--min-events", type=int, default=MIN_EVENTS, help="may only raise the floor of 30")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)
    parts = {p.strip().upper() for p in a.parts.split(",") if p.strip()}
    depths = [float(x) for x in a.depths.split(",") if x.strip()]
    horizons = [int(x) for x in a.horizons.split(",") if x.strip()]
    if not depths or not horizons or any(d <= 0 for d in depths) or any(h < 1 for h in horizons):
        print("need positive --depths and --horizons >= 1", file=sys.stderr)
        return 2
    min_events = max(MIN_EVENTS, a.min_events)
    rep: Dict[str, Any] = {"variants_tried": 0}
    ran = False
    if "A" in parts:
        if not a.daily or not Path(a.daily).is_dir():
            print("part A needs --daily HIST_DIR", file=sys.stderr)
            rep.setdefault("skipped", {})["A"] = ("--daily HIST_DIR is missing or not a folder" if not a.daily else f"{a.daily} is not a folder") + \
                " (scripts/e278_run.sh fetches the history and passes it)"
        else:
            rep["A"] = run_part_a(Path(a.daily), depths, horizons, a.cost_pct, a.slip_pct, a.boot, min_events, a.sample, a.seed)
            rep["variants_tried"] += len(depths) * len(horizons)
            ran = True
    if parts & {"B", "C"}:
        if not a.bars or not Path(a.bars).is_dir():
            print("parts B and C need --bars BAR_DIR", file=sys.stderr)
            for pt in sorted(parts & {"B", "C"}):
                rep.setdefault("skipped", {})[pt] = "--bars BAR_DIR is missing or not a folder"
        else:
            if "B" in parts:
                rep["B"] = run_part_b(Path(a.bars), min_events)
                rep["variants_tried"] += 1
                ran = True
            if "C" in parts:
                rep["C"] = run_part_c(Path(a.bars), a.ic_cost_pct, a.boot, min_events)
                rep["variants_tried"] += len(IC_HORIZONS)
                ran = True
    md = to_markdown(rep)
    if a.out:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "sweep_study.json").write_text(json.dumps(rep, indent=1))
        (out / "sweep_study.md").write_text(md)
    print(md)
    return 0 if ran else 1


if __name__ == "__main__":
    sys.exit(main())
