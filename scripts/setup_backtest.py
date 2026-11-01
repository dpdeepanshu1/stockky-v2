#!/usr/bin/env python3
"""setup_backtest.py - E272: offline replay of the setups S1 to S4 on the recorded 1-minute bars, with a day-clustered bootstrap (read-only, stdlib only).

    python3 scripts/setup_backtest.py BAR_DIR [--days 20] [--cost-pct 0.15] [--hold 60] [--events events.json] [--boot 2000] [--out DIR]

For every symbol-day it builds the facts the REAL orders/setup_engine.py reads (the same file the live code runs, loaded by path), asks classify() at each setup's
first allowed minute (S1 / S4 09:21, S2 09:30, S3 09:45) and, when a setup wins, enters at the open of the NEXT 1-minute bar and exits --hold minutes later (net of --cost-pct).
It reports per setup: n, days, mean net %, win %, a day-clustered bootstrap 90 % interval of the mean (days are resampled, not trades, because all stocks of a day
share the market) and the verdict edge / no_edge / unclear / insufficient_sample. This is what the first 30-trade cells are checked against.

WHAT BARS CAN AND CANNOT GIVE (read before trusting it)
  * S1 / S4 need a typed event. Bars hold none, so they run only with --events FILE: {"YYYY-MM-DD": {"SYMBOL": {"polarity": 1, "strength": 0.7, "priced_in": 0.4}}}
    (for example exported from the event feed). Without it they report n = 0, never a made-up event.
  * There is no tape in bars. `tape` is a PROXY: sign of the last closed bar (close vs open). Sector third is not available (left unknown, so a confirmation is never claimed).
  * S2: rs_pct = the symbol's rank of its return since the open among all symbols that day; structure_ok = previous close above its 5-day mean close; candle = hammer / shooting
    star on the last closed bar; vol_vs_norm = volume since the open against the same minutes' median on the OTHER days (needs 3+ other days, else unknown).
  * S3: z_move = move since the open in units of the symbol's own median 15-minute range; failure_candle = last bar closes back against the move; tape_exhaustion = the
    proxy tape turned against the move; `liquid` = the symbol's median daily volume is in the top half of the sample. Climax volume / VWAP stretch use the bars' VWAP.
  * Fixed-horizon exits only: no stop, no target, no slippage beyond --cost-pct. With only a few recorded days every interval is wide; read direction, not proof.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _intraday_data as idata  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
_spec = importlib.util.spec_from_file_location("setup_engine_bt", HERE.parent / "services" / "position-stocks-service" / "orders" / "setup_engine.py")
se = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(se)

DECISION_OFF = {se.S1: 6, se.S4: 6, se.S2: 15, se.S3: 30}      # minutes after 09:15 (09:21, 09:30, 09:45)


def _mm(bars, date: str) -> Dict[int, tuple]:
    t0 = idata.open_ts(date)
    out: Dict[int, tuple] = {}
    for b in bars:
        off = int(round((b[0] - t0) / 60.0))
        if 0 <= off < 375:
            out[off] = b
    return out


def _clock(date: str, off: int) -> datetime:
    return datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=IST, hour=9, minute=15) + timedelta(minutes=off)


def _candle(b: tuple) -> int:
    """+1 hammer (long lower wick), -1 shooting star (long upper wick), else 0."""
    o, h, l, c = b[1], b[2], b[3], b[4]
    rng = h - l
    if rng <= 0:
        return 0
    body = abs(c - o)
    lower, upper = min(o, c) - l, h - max(o, c)
    if lower >= 2 * body and lower >= 0.5 * rng:
        return 1
    if upper >= 2 * body and upper >= 0.5 * rng:
        return -1
    return 0


def _vwap(mb: Dict[int, tuple], upto: int) -> Optional[float]:
    pv = v = 0.0
    for o in range(upto + 1):
        b = mb.get(o)
        if b and b[5] > 0:
            tp = (b[2] + b[3] + b[4]) / 3.0
            pv += tp * b[5]
            v += b[5]
    return pv / v if v > 0 else None


def _day_ctx(data: Dict[str, Dict[str, Any]], dates: List[str]) -> Dict[str, Any]:
    """Per-symbol facts that need other days: previous close, 5-day mean close, median 15-min range, median daily volume, median cumulative volume by minute."""
    ctx: Dict[str, Any] = {}
    for sym, days in data.items():
        comp = sorted(d for d, x in days.items() if x.get("complete"))
        ranges, dvols, cum_by_off = [], [], defaultdict(list)
        for d in comp:
            mb = _mm(days[d]["bars"], d)
            dvols.append(sum(b[5] for b in mb.values()))
            for a in range(0, 360, 15):
                bs = [mb[o] for o in range(a, a + 15) if o in mb]
                if len(bs) >= 10 and (hi := max(b[2] for b in bs)) > 0:
                    ranges.append((hi - min(b[3] for b in bs)) / hi * 100.0)
            run = 0.0
            for o in range(0, 375):
                run += mb[o][5] if o in mb else 0.0
                cum_by_off[(d, o)] = run
        ctx[sym] = {"comp": comp, "range15": statistics.median(ranges) if ranges else None, "dvol": statistics.median(dvols) if dvols else None, "cum": cum_by_off}
    return ctx


def replay(data: Dict[str, Dict[str, Any]], *, since: Optional[str], cost_pct: float, hold: int, events: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """One dict per taken trade: {date, symbol, setup, side, slot, net}. Pure function of the bars (and the optional events)."""
    events = events or {}
    ctx = _day_ctx(data, [])
    vols = sorted(c["dvol"] for c in ctx.values() if c["dvol"])
    liquid_cut = vols[len(vols) // 2] if vols else None
    dates = sorted({d for days in data.values() for d, x in days.items() if x.get("complete")})
    trades: List[Dict[str, Any]] = []
    for date in dates:
        if since and date < since:
            continue
        snaps: Dict[str, Dict[str, Any]] = {}
        for sym, days in data.items():
            day = days.get(date)
            if not day or not day.get("complete"):
                continue
            prev = idata.previous_day(days, date)
            if not prev or not days[prev].get("complete"):
                continue
            mb = _mm(day["bars"], date)
            b0 = mb.get(0)
            pc = days[prev]["bars"][-1][4]
            if b0 is None or pc <= 0 or b0[1] <= 0 or abs((b0[1] / pc - 1.0) * 100.0) > 25.0:
                continue
            c = ctx[sym]
            hist = [d for d in c["comp"] if d < date][-5:]
            closes = [days[d]["bars"][-1][4] for d in hist]
            snaps[sym] = {"mb": mb, "open": b0[1], "pc": pc, "gap": (b0[1] / pc - 1.0) * 100.0, "structure": bool(closes and pc > sum(closes) / len(closes)), "c": c}
        for sid, off in DECISION_OFF.items():
            # relative strength of the day at this minute, among all symbols with a bar then
            ret = {s: (v["mb"][off - 1][4] / v["open"] - 1.0) for s, v in snaps.items() if (off - 1) in v["mb"] and off in v["mb"]}
            ranked = sorted(ret, key=lambda s: ret[s])
            rs = {s: (i + 1) / len(ranked) for i, s in enumerate(ranked)}
            for sym, v in snaps.items():
                mb = v["mb"]
                if off not in mb or (off - 1) not in mb or (off + hold) not in mb and not any((off + hold - k) in mb for k in range(1, 6)):
                    continue
                last = mb[off - 1]
                hi5 = max(mb[o][2] for o in range(5) if o in mb) if any(o in mb for o in range(5)) else None
                lo5 = min(mb[o][3] for o in range(5) if o in mb) if any(o in mb for o in range(5)) else None
                if hi5 is None:
                    continue
                move = (last[4] / v["open"] - 1.0) * 100.0
                r15 = v["c"]["range15"]
                vw = _vwap(mb, off - 1)
                others = [d for d in v["c"]["comp"] if d != date]
                cur_cum = sum(mb[o][5] for o in range(off) if o in mb)
                base = [v["c"]["cum"][(d, off - 1)] for d in others if (d, off - 1) in v["c"]["cum"] and v["c"]["cum"][(d, off - 1)] > 0]
                vnorm = (cur_cum / statistics.median(base)) if len(base) >= 3 else None
                tape = 1 if last[4] > last[1] else (-1 if last[4] < last[1] else 0)
                ev = (events.get(date) or {}).get(sym) or {}
                z = (move / r15) if r15 else None
                facts: Dict[str, Any] = {
                    "service": "PS", "now": _clock(date, off), "price": last[4], "preopen_price": v["open"], "or_high": hi5, "or_low": lo5, "bar_close": last[4],
                    "bar_closed_after_range": True, "above_vwap": (last[4] > vw) if vw else None, "tape": tape,
                    "event_polarity": ev.get("polarity"), "event_priced_in": ev.get("priced_in"),
                    "rs_pct": rs.get(sym), "gap_pct": v["gap"], "structure_ok": v["structure"],
                    "or_break": (1 if last[4] > hi5 else (-1 if last[4] < lo5 else 0)), "vol_vs_norm": vnorm, "candle": _candle(last),
                    "z_move": z, "failure_candle": (last[4] < last[1]) if move > 0 else (last[4] > last[1]),
                    "tape_exhaustion": (tape < 0) if move > 0 else (tape > 0), "liquid": bool(liquid_cut and v["c"]["dvol"] and v["c"]["dvol"] >= liquid_cut),
                    "kind": "panic_dump" if move < 0 else "spike", "vwap_stretch": bool(vw and abs(last[4] / vw - 1.0) * 100.0 > (r15 or 1e9) * 0.5),
                    "climax_volume": bool(last[5] > 0 and vnorm and v["c"]["dvol"] and last[5] > 3 * v["c"]["dvol"] / 375.0),
                    "allowed": {sid},
                }
                for side in ("LONG", "SHORT"):
                    f = dict(facts, side=side)
                    r = se.classify(f)
                    if r.get("id") != sid:
                        continue
                    entry_b = mb.get(off)
                    exit_off = next((off + hold - k for k in range(0, 6) if (off + hold - k) in mb), None)
                    if entry_b is None or exit_off is None or entry_b[1] <= 0:
                        continue
                    sgn = 1 if side == "LONG" else -1
                    gross = sgn * (mb[exit_off][4] / entry_b[1] - 1.0) * 100.0
                    trades.append({"date": date, "symbol": sym, "setup": sid, "side": side, "slot": se.slot(f["now"]), "net": gross - cost_pct})
    return trades


def bootstrap(pairs: List[tuple], n_boot: int = 2000, seed: int = 7) -> Dict[str, Any]:
    """Day-clustered bootstrap of the mean net: resample DAYS with replacement, each day contributing its mean. pairs = [(date, net)]. 90 % interval."""
    by: Dict[str, List[float]] = defaultdict(list)
    for d, v in pairs:
        by[d].append(v)
    dm = [sum(x) / len(x) for x in by.values()]
    if len(dm) < 2:
        return {"lo": None, "hi": None}
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(dm) for _ in dm) / len(dm) for _ in range(n_boot))
    return {"lo": round(means[int(0.05 * n_boot)], 4), "hi": round(means[int(0.95 * n_boot) - 1], 4)}


def summarize(trades: List[Dict[str, Any]], min_n: int = 30, min_days: int = 5, n_boot: int = 2000) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in trades:
        groups[f"{t['setup']}_{t['side']}"].append(t)
    for sid in (se.S1, se.S4, se.S2, se.S3):
        for side in ("LONG", "SHORT"):
            if (sid == se.S1 and side == "SHORT") or (sid == se.S4 and side == "LONG"):
                continue
            rows = groups.get(f"{sid}_{side}", [])
            n = len(rows)
            days = len({r["date"] for r in rows})
            if n == 0:
                out[f"{sid}_{side}"] = {"n": 0, "days": 0, "verdict": "insufficient_sample"}
                continue
            vals = [r["net"] for r in rows]
            ci = bootstrap([(r["date"], r["net"]) for r in rows], n_boot)
            lo, hi = ci["lo"], ci["hi"]
            verdict = ("insufficient_sample" if n < min_n or days < min_days or lo is None else "edge" if lo > 0 else "no_edge" if hi < 0 else "unclear")
            out[f"{sid}_{side}"] = {"n": n, "days": days, "mean": round(sum(vals) / n, 4), "win_pct": round(100.0 * sum(1 for v in vals if v > 0) / n, 1),
                                    "ci90_lo": lo, "ci90_hi": hi, "verdict": verdict}
    return out


def render(rep: Dict[str, Dict[str, Any]], args) -> str:
    lines = [f"# Setup replay (hold {args.hold} min, cost {args.cost_pct}%, day-clustered bootstrap 90 % interval)", "",
             "| Setup | n | days | mean net % | win % | 90 % interval | verdict |", "|---|---|---|---|---|---|---|"]
    for k, r in rep.items():
        lines.append(f"| {k} | {r['n']} | {r['days']} | {r.get('mean', '-')} | {r.get('win_pct', '-')} | {r.get('ci90_lo', '-')} to {r.get('ci90_hi', '-')} | {r['verdict']} |")
    lines += ["", "S1 / S4 need --events; tape, sector and structure are proxies (see the docstring). Direction, not proof."]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Offline replay of S1 to S4 on recorded bars (read-only)")
    ap.add_argument("bars_dir", nargs="?", default="/data/bars")
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--cost-pct", type=float, default=0.15)
    ap.add_argument("--hold", type=int, default=60, help="minutes held after entry")
    ap.add_argument("--events", default="", help="JSON {date: {symbol: {polarity, strength, priced_in}}} for S1 / S4")
    ap.add_argument("--min-n", type=int, default=30)
    ap.add_argument("--min-days", type=int, default=5)
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    data = idata.load_dir(a.bars_dir) if Path(a.bars_dir).is_dir() else {}
    if not data:
        print(f"nothing to do: no bar folders under {a.bars_dir}", file=sys.stderr)
        return 2
    events = None
    if a.events:
        try:
            events = json.loads(Path(a.events).read_text())
        except Exception as e:  # noqa: BLE001
            print(f"events file not read: {e}", file=sys.stderr)
    dates = sorted({d for days in data.values() for d in days})
    since = dates[-a.days] if a.days > 0 and len(dates) > a.days else None
    rep = summarize(replay(data, since=since, cost_pct=a.cost_pct, hold=a.hold, events=events), a.min_n, a.min_days, a.boot)
    md = render(rep, a)
    print(md)
    if a.out:
        o = Path(a.out)
        o.mkdir(parents=True, exist_ok=True)
        (o / "setup_backtest.md").write_text(md)
        (o / "setup_backtest.json").write_text(json.dumps(rep, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
