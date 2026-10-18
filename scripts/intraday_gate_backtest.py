#!/usr/bin/env python3
"""intraday_gate_backtest.py - group 441: do the VWAP gate and the opening gate earn their place? Backtested on the RECORDED 1-minute bars.

  python3 scripts/intraday_gate_backtest.py BAR_DIR [--out DIR] [--horizon 30] [--every 5] [--until 14:30] [--cost-pct 0.12] [--slip-pct 0.05]
                                            [--min-n 200] [--min-sessions 20] [--boot 500] [--thresholds '{}'] [--symbols A,B] [--include-partial]

BAR_DIR is the folder services/position-stocks-service/feed/bar_recorder.py writes (one folder per IST day, one file per symbol). Daily candles cannot test
these two gates (they are intraday rules), which is why the minute bars are recorded at all.

WHAT IS REPLAYED. Every `--every` minutes from 09:15 to `--until`, for every recorded symbol-day, one hypothetical entry is taken - NOT only the entries the
bot happened to make. The decision at clock time t sees ONLY minutes that finished before t; the entry is the OPEN of the first minute at or after t
(+ slippage); the exit is the close of the last finished minute before t + horizon (15 / 30 / 60 minutes; an exit after 15:25 has no return). Round-trip cost
is subtracted. Nothing after t is ever read by a rule.
  VWAP gate   (entry_engine/vwap_gate.py)   reject when price < vwap * (1 - tolerance/100); VWAP rebuilt from today's minutes (typical price x volume); no VWAP,
                                            or a VWAP more than 15 % from the price, is "no opinion" (passes). The live gate reads Dhan's own average price: close approximation.
  Opening gate (entry_engine/opening_gate.py) active from the open to --settle minutes after it (45 = 10:00): too early, price vs previous close outside
                                            [min, max] %, price in the top part of the day range (after range_after minutes), previous day closed in its lower half.
                                            NOT replayed: the stop-vs-ATR check. Entries with no recorded previous day (within 6 calendar days) are left out.
Only COMPLETE sessions are used (started within 5 minutes of the open, >= 80 % of 375 minutes). `--include-partial` switches that off (not for evidence).

VERDICT. baseline = every entry the gate looks at, candidate = those it lets through, edge = candidate mean net - baseline mean net (pp). Cluster bootstrap with
one cluster per SESSION (symbols of a day share the market's move; seed 7).
  no_data | insufficient_sample (< --min-n entries either side, or < --min-sessions sessions) | pass (edge > 0, bootstrap 5th pct > 0, both halves positive,
  candidate mean net > 0) | hurts (95th pct < 0) | no_edge
A `pass` is evidence on the recorded weeks only. The tool changes no setting.
Output: gate_backtest.json + gate_backtest.md in --out (default BAR_DIR/gate_out). Stdlib only, deterministic.
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _intraday_data as data  # noqa: E402
import ms_ab_report as ab  # noqa: E402

HORIZONS_MIN = (15, 30, 60)
SESSION_END_MIN = 375
LAST_EXIT_MIN = 370                  # 15:25
MAX_STALE_S = 300
MAX_ENTRY_GAP_S = 180

DEFAULTS: Dict[str, float] = {
    "vwap_tol_pct": 0.15, "vwap_max_dist_pct": 15.0,
    "og_settle_min": 45.0, "og_min_minutes": 0.0, "og_min_chg": -0.5, "og_max_chg": 3.0,
    "og_max_range_pos": 0.88, "og_range_after_min": 15.0, "og_min_prev_close_pos": 0.5,
}
OG_RULES = ("og_too_early", "og_below_prev_close", "og_extended", "og_near_day_high", "og_prev_weak_close")


def vwap_flag(price: float, vwap: Optional[float], tol_pct: float, max_dist_pct: float) -> bool:
    """True = the live VWAP gate would reject. No / non-positive VWAP, or one too far from the price, is no opinion."""
    if vwap is None or vwap <= 0 or price <= 0:
        return False
    if abs(price - vwap) / vwap * 100.0 > max_dist_pct:
        return False
    return price < vwap * (1.0 - tol_pct / 100.0)


def opening_flags(minutes_open: float, price: float, prev_close: float, day_high: float, day_low: float, prev_close_pos: Optional[float],
                  th: Dict[str, float]) -> List[str]:
    """The opening-gate rules that reject at this moment (empty when none, or outside the gate window). Same order and numbers as opening_gate.reject_reason."""
    if minutes_open < 0 or minutes_open >= th["og_settle_min"]:
        return []
    out: List[str] = []
    if minutes_open < th["og_min_minutes"]:
        out.append("og_too_early")
    chg = (price - prev_close) / prev_close * 100.0
    if chg < th["og_min_chg"]:
        out.append("og_below_prev_close")
    if chg > th["og_max_chg"]:
        out.append("og_extended")
    if th["og_max_range_pos"] > 0 and minutes_open >= th["og_range_after_min"]:
        if day_high > day_low:
            rpos = max(0.0, min(1.0, (price - day_low) / (day_high - day_low)))
            if rpos >= th["og_max_range_pos"]:
                out.append("og_near_day_high")
        else:
            out.append("og_near_day_high")                  # a flat range is "NO_DAY_RANGE" in the live gate: it holds the entry back
    if th["og_min_prev_close_pos"] > 0 and prev_close_pos is not None and prev_close_pos < th["og_min_prev_close_pos"]:
        out.append("og_prev_weak_close")
    return out


def _prev_stats(bars: Sequence[Sequence[float]]) -> Optional[Dict[str, float]]:
    if not bars:
        return None
    hi, lo, cl = max(b[2] for b in bars), min(b[3] for b in bars), bars[-1][4]
    span = hi - lo
    return {"close": cl, "high": hi, "low": lo, "close_pos": max(0.0, min(1.0, (cl - lo) / span)) if span > 1e-9 else None}   # None = zero-range day, as PrevDay.close_pos


def entry_rows(sym: str, date: str, bars: Sequence[Sequence[float]], prev_bars: Optional[Sequence[Sequence[float]]], th: Dict[str, float],
               every: int = 5, until_min: int = 315, cost_pct: float = 0.12, slip_pct: float = 0.05) -> List[Dict[str, Any]]:
    """Hypothetical entries for one symbol-day (see the module doc). Pure."""
    n = len(bars)
    if n < 20:
        return []
    o_ts = data.open_ts(date)
    ts = [b[0] for b in bars]
    cum_pv, cum_v, run_hi, run_lo = [], [], [], []
    pv = vv = 0.0
    hi, lo = 0.0, float("inf")
    for b in bars:
        pv += (b[2] + b[3] + b[4]) / 3.0 * max(0.0, b[5])
        vv += max(0.0, b[5])
        hi, lo = max(hi, b[2]), min(lo, b[3])
        cum_pv.append(pv)
        cum_v.append(vv)
        run_hi.append(hi)
        run_lo.append(lo)
    prev = _prev_stats(prev_bars) if prev_bars else None
    rows: List[Dict[str, Any]] = []
    for m in range(0, int(until_min) + 1, max(1, int(every))):
        t = o_ts + m * 60.0
        k = bisect.bisect_left(ts, t)                       # bars [0, k) finished before t
        if k < 1 or k >= n:
            continue
        last = k - 1
        if t - ts[last] > MAX_STALE_S or ts[k] - t > MAX_ENTRY_GAP_S:
            continue
        price = bars[last][4]
        vwap = cum_pv[last] / cum_v[last] if cum_v[last] > 0 else None
        entry = bars[k][1] * (1.0 + slip_pct / 100.0)
        if entry <= 0 or price <= 0:
            continue
        row: Dict[str, Any] = {"sym": sym, "day": date, "m": m, "ts": t, "vwap_dist_pct": round((price / vwap - 1.0) * 100.0, 3) if vwap else None,
                               "rej_vwap": vwap_flag(price, vwap, th["vwap_tol_pct"], th["vwap_max_dist_pct"]), "og": None, "chg": None}
        if prev is not None:
            row["chg"] = round((price - prev["close"]) / prev["close"] * 100.0, 3)
            if m < th["og_settle_min"]:
                row["og"] = opening_flags(float(m), price, prev["close"], run_hi[last], run_lo[last], prev["close_pos"], th)
        got = False
        for h in HORIZONS_MIN:
            r = None
            if m + h <= LAST_EXIT_MIN:
                j = bisect.bisect_left(ts, t + h * 60.0) - 1
                if j >= k and (t + h * 60.0) - ts[j] <= MAX_STALE_S:
                    r = round((bars[j][4] / entry - 1.0) * 100.0 - cost_pct, 3)
                    got = True
            row[f"r{h}"] = r
        if got:
            rows.append(row)
    return rows


def collect(root: str, th: Dict[str, float], every: int, until_min: int, cost: float, slip: float, symbols: Optional[List[str]] = None,
            include_partial: bool = False) -> Dict[str, Any]:
    st: Dict[str, int] = {}
    days = data.load_dir(root, symbols, st)
    rows: List[Dict[str, Any]] = []
    used = skipped_partial = no_prev = 0
    for sym, per in sorted(days.items()):
        for date, d in sorted(per.items()):
            if not (d["complete"] or include_partial):
                skipped_partial += 1
                continue
            pd = data.previous_day(per, date)
            pbars = per[pd]["bars"] if (pd is not None and (per[pd]["complete"] or include_partial)) else None
            if pbars is None:
                no_prev += 1
            used += 1
            rows += entry_rows(sym, date, d["bars"], pbars, th, every, until_min, cost, slip)
    sessions = sorted({r["day"] for r in rows})
    return {"rows": rows, "files": st.get("files", 0), "bad_files": st.get("bad_files", 0), "symbols": len(days), "symbol_days_used": used,
            "symbol_days_partial_skipped": skipped_partial, "symbol_days_without_prev_day": no_prev, "sessions": sessions}


def _view(r: Dict[str, Any], rej: List[str]) -> Dict[str, Any]:
    v = dict(r)
    v["sym"], v["month"], v["rej"], v["ts"] = "*", r["day"], rej, r["ts"]          # one bootstrap cluster per session
    return v


def _stat(rows: Sequence[Dict[str, Any]], h: int) -> Dict[str, Any]:
    x = [r[f"r{h}"] for r in rows if r.get(f"r{h}") is not None]
    if not x:
        return {"n": 0}
    return {"n": len(x), "mean_net": round(mean(x), 3), "win_pct": round(100.0 * sum(1 for v in x if v > 0) / len(x), 1)}


def gate_report(views: List[Dict[str, Any]], horizon: int, min_n: int, min_sessions: int, n_boot: int, rules: Sequence[str]) -> Dict[str, Any]:
    base = [v for v in views if v.get(f"r{horizon}") is not None]
    cand = [v for v in base if not v["rej"]]
    rej = [v for v in base if v["rej"]]
    sessions = sorted({v["month"] for v in base})
    cut = sessions[len(sessions) // 2] if sessions else ""
    halves = [[v for v in base if v["month"] < cut], [v for v in base if v["month"] >= cut]]
    half_edges = [ab._edge(h_, [v for v in h_ if not v["rej"]], horizon) for h_ in halves]
    edge = ab._edge(base, cand, horizon)
    boot = ab.bootstrap_edge(base, horizon, n_boot)
    cs = _stat(cand, horizon)
    if not base:
        verdict = "no_data"
    elif len(sessions) < min_sessions or boot.get("ci90") is None:
        verdict = "insufficient_sample"
    else:
        verdict = ab.verdict(len(cand), len(base), edge, boot.get("ci90"), half_edges, cs.get("mean_net"), min_n)
    rep: Dict[str, Any] = {"entries": {"baseline": len(base), "candidate": len(cand), "rejected": len(rej)}, "sessions": len(sessions),
                           "rejected_pct": round(100.0 * len(rej) / len(base), 1) if base else None,
                           "baseline": _stat(base, horizon), "candidate": cs, "rejected": _stat(rej, horizon), "edge_pp": edge, "bootstrap": boot,
                           "halves": {"first": half_edges[0], "second": half_edges[1], "cut": cut or None},
                           "by_horizon": {str(h): {"baseline": _stat(base, h), "candidate": _stat([v for v in base if not v["rej"]], h),
                                                   "edge_pp": ab._edge([v for v in views if v.get(f"r{h}") is not None],
                                                                       [v for v in views if v.get(f"r{h}") is not None and not v["rej"]], h)} for h in HORIZONS_MIN},
                           "by_rule": {}, "verdict": verdict}
    for rule in rules:
        only = [v for v in base if rule in v["rej"]]
        solo = [v for v in base if rule not in v["rej"]]
        no_other = [v for v in base if not (set(v["rej"]) - {rule})]
        rep["by_rule"][rule] = {"rejected": _stat(only, horizon), "share_pct": round(100.0 * len(only) / len(base), 1) if base else None,
                                "solo_edge_pp": ab._edge(base, solo, horizon), "drop_one_edge_pp": ab._edge(no_other, cand, horizon), "drop_one_n": len(no_other)}
    return rep


def clock_buckets(rows: Sequence[Dict[str, Any]], horizon: int, width: int = 30) -> List[Dict[str, Any]]:
    """Mean net return of EVERY sampled entry by minutes-since-open bucket (a read-out, not a gate)."""
    out: Dict[int, List[float]] = {}
    for r in rows:
        v = r.get(f"r{horizon}")
        if v is not None:
            out.setdefault(int(r["m"] // width), []).append(v)
    res = []
    for b in sorted(out):
        x = out[b]
        hh = 9 * 60 + 15 + b * width
        res.append({"from": f"{hh // 60:02d}:{hh % 60:02d}", "n": len(x), "mean_net": round(mean(x), 3), "win_pct": round(100.0 * sum(1 for v in x if v > 0) / len(x), 1)})
    return res


def build_report(coll: Dict[str, Any], horizon: int, min_n: int, min_sessions: int, n_boot: int, meta: Dict[str, Any]) -> Dict[str, Any]:
    rows = coll["rows"]
    vw = [_view(r, ["vwap_below"] if r["rej_vwap"] else []) for r in rows]
    op = [_view(r, list(r["og"])) for r in rows if r["og"] is not None]
    return {"meta": meta, "horizon": horizon,
            "data": {k: coll[k] for k in ("files", "bad_files", "symbols", "symbol_days_used", "symbol_days_partial_skipped", "symbol_days_without_prev_day")},
            "sessions": {"count": len(coll["sessions"]), "first": coll["sessions"][0] if coll["sessions"] else None,
                         "last": coll["sessions"][-1] if coll["sessions"] else None},
            "entries_sampled": len(rows),
            "vwap_gate": gate_report(vw, horizon, min_n, min_sessions, n_boot, ("vwap_below",)),
            "opening_gate": gate_report(op, horizon, min_n, min_sessions, n_boot, OG_RULES),
            "by_clock": clock_buckets(rows, horizon)}


def to_markdown(rep: Dict[str, Any]) -> str:
    f = lambda v: "-" if v is None else str(v)  # noqa: E731
    d, s, m = rep["data"], rep["sessions"], rep["meta"]
    L: List[str] = [f"# Intraday gate backtest (T+{rep['horizon']} min, net of costs)", "",
                    f"- recorded: {d['files']} files ({d['bad_files']} unreadable), {d['symbols']} symbols, {s['count']} sessions ({f(s['first'])} .. {f(s['last'])})",
                    f"- symbol-days used {d['symbol_days_used']}, started late / partial (left out) {d['symbol_days_partial_skipped']}, without a previous day {d['symbol_days_without_prev_day']}",
                    f"- {rep['entries_sampled']} hypothetical entries, one every {m['every']} min until {m['until']}; cost {m['cost_pct']}% + {m['slip_pct']}% slippage", ""]
    for key, title in (("vwap_gate", "VWAP gate"), ("opening_gate", "Opening gate (09:15 - settle)")):
        g = rep[key]
        e = g["entries"]
        L += [f"## {title}", "", f"**Verdict: `{g['verdict']}`**", "",
              f"- entries: baseline {e['baseline']}, passed {e['candidate']}, rejected {e['rejected']} ({f(g['rejected_pct'])}%), sessions {g['sessions']}",
              f"- edge (passed - baseline mean net): **{f(g['edge_pp'])} pp**, 90% CI {f(g['bootstrap']['ci90'])}, P(edge>0) {f(g['bootstrap']['p_edge_positive'])}, session clusters {g['bootstrap']['clusters']}",
              f"- halves (cut {f(g['halves']['cut'])}): first {f(g['halves']['first'])} pp, second {f(g['halves']['second'])} pp", "",
              "| group | n | mean net % | win % |", "|---|---|---|---|"]
        for nm in ("baseline", "candidate", "rejected"):
            x = g[nm]
            L.append(f"| {nm} | {x.get('n', 0)} | {f(x.get('mean_net'))} | {f(x.get('win_pct'))} |")
        L += ["", "| T+ min | baseline mean | passed mean | edge pp |", "|---|---|---|---|"]
        for h, x in g["by_horizon"].items():
            L.append(f"| {h} | {f(x['baseline'].get('mean_net'))} | {f(x['candidate'].get('mean_net'))} | {f(x['edge_pp'])} |")
        L += ["", "| rule | share of entries % | n | mean net % | solo edge pp | drop-one edge pp |", "|---|---|---|---|---|---|"]
        for k, x in g["by_rule"].items():
            L.append(f"| {k} | {f(x['share_pct'])} | {x['rejected'].get('n', 0)} | {f(x['rejected'].get('mean_net'))} | {f(x['solo_edge_pp'])} | {f(x['drop_one_edge_pp'])} |")
        L.append("")
    L += ["## All sampled entries by time of day", "", "| from (IST) | n | mean net % | win % |", "|---|---|---|---|"]
    for b in rep["by_clock"]:
        L.append(f"| {b['from']} | {b['n']} | {b['mean_net']} | {b['win_pct']} |")
    L += ["", "Entries are sampled every few minutes, so neighbours overlap: the bootstrap clusters by session and `insufficient_sample` means the recorded period is too short "
              "to conclude anything. The stop-vs-ATR opening check is not replayed. Nothing here changes a setting."]
    return "\n".join(L) + "\n"


def _hhmm_to_min(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m) - (9 * 60 + 15)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir")
    ap.add_argument("--out", default="")
    ap.add_argument("--horizon", type=int, default=30, choices=HORIZONS_MIN)
    ap.add_argument("--every", type=int, default=5)
    ap.add_argument("--until", default="14:30")
    ap.add_argument("--cost-pct", type=float, default=0.12)
    ap.add_argument("--slip-pct", type=float, default=0.05)
    ap.add_argument("--min-n", type=int, default=200)
    ap.add_argument("--min-sessions", type=int, default=20)
    ap.add_argument("--boot", type=int, default=500)
    ap.add_argument("--thresholds", default="{}")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--include-partial", action="store_true")
    a = ap.parse_args(argv)
    th = dict(DEFAULTS)
    th.update({k: float(v) for k, v in json.loads(a.thresholds).items() if k in DEFAULTS})
    until_min = max(0, min(SESSION_END_MIN - 1, _hhmm_to_min(a.until)))
    syms = [s.strip() for s in a.symbols.split(",") if s.strip()] or None
    coll = collect(a.dir, th, a.every, until_min, a.cost_pct, a.slip_pct, syms, a.include_partial)
    meta = {"every": a.every, "until": a.until, "cost_pct": a.cost_pct, "slip_pct": a.slip_pct, "thresholds": th, "include_partial": a.include_partial,
            "min_n": a.min_n, "min_sessions": a.min_sessions}
    rep = build_report(coll, a.horizon, a.min_n, a.min_sessions, a.boot, meta)
    out = Path(a.out) if a.out else Path(a.dir) / "gate_out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "gate_backtest.json").write_text(json.dumps(rep, indent=1))
    md = to_markdown(rep)
    (out / "gate_backtest.md").write_text(md)
    print(md)
    if not coll["rows"]:
        print("NO ENTRIES: the folder has no complete recorded session yet (see the data line above); keep the recorder running", file=sys.stderr)
    return 0 if coll["rows"] else 1


if __name__ == "__main__":
    sys.exit(main())
