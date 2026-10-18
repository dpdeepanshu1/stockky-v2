#!/usr/bin/env python3
"""export_intraday.py - group 441 (WS-15): turn the recorded 1-minute bars into intraday (15m / 5m) structure rows, with what happened next.

  python3 scripts/export_intraday.py BAR_DIR [--out DIR] [--tf 15,5] [--step 2] [--symbols A,B] [--all-quality] [--include-partial]

BAR_DIR = the folder feed/bar_recorder.py writes (compose volume `bar-records`, mounted at /data/bars in position-stocks-service). Recorded days of a symbol
are chained, aggregated into session-aligned N-minute bars and replayed with NO lookahead (market_structure.intraday.replay_intraday). One JSON line per
replayed bar goes to intraday_rows_<tf>m.jsonl:
  {sym, date, ts, tf, price, quality, session_complete, summary: {i15_trend, ...}, fwd: {r4, r8, mfe8, mae8}}
`fwd` = a long at the NEXT bar's open over the next 4 / 8 bars of the SAME session (gross %, no costs; None when the session ends first).
Bars of a session that started late are kept only with --include-partial and marked session_complete=false. Rows with quality insufficient / stale are
dropped unless --all-quality. Also written: coverage.json / coverage.md so a thin recording is visible BEFORE anyone reads a result.
Read-only on BAR_DIR. Stdlib only, deterministic.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _intraday_data as data  # noqa: E402
from market_structure import intraday as it  # noqa: E402
from market_structure.ms_types import C, H, IST_OFFSET_S, L, O, T  # noqa: E402

FWD_BARS = (4, 8)


def _day(ts: float) -> int:
    return int((ts + IST_OFFSET_S) // 86400)


def forward(agg: Sequence[Sequence[float]], i: int) -> Dict[str, Any]:
    """Gross forward behaviour of a long at the open of bar i+1 (same session only)."""
    out: Dict[str, Any] = {f"r{n}": None for n in FWD_BARS}
    out.update({"mfe8": None, "mae8": None})
    if i + 1 >= len(agg):
        return out
    entry = float(agg[i + 1][O])
    if entry <= 0:
        return out
    d = _day(agg[i + 1][T])
    win = []
    for b in agg[i + 1: i + 1 + max(FWD_BARS)]:
        if _day(b[T]) != d:
            break
        win.append(b)
    for n in FWD_BARS:
        if len(win) >= n:
            out[f"r{n}"] = round((float(win[n - 1][C]) / entry - 1.0) * 100.0, 3)
    if len(win) >= max(FWD_BARS):
        out["mfe8"] = round((max(float(b[H]) for b in win) / entry - 1.0) * 100.0, 3)
        out["mae8"] = round(min(0.0, (min(float(b[L]) for b in win) / entry - 1.0) * 100.0), 3)
    return out


def symbol_rows(sym: str, per_day: Dict[str, Dict[str, Any]], tf: int, step: int, all_quality: bool, include_partial: bool) -> List[Dict[str, Any]]:
    minutes: List[Sequence[float]] = []
    complete_by_day: Dict[int, bool] = {}
    for date, d in sorted(per_day.items()):
        if not (d["complete"] or include_partial):
            continue
        minutes += d["bars"]
        complete_by_day[_day(data.open_ts(date))] = bool(d["complete"])
    agg = it.aggregate_session(minutes, tf)
    if len(agg) < it.intraday_params(tf).min_bars:
        return []
    rows: List[Dict[str, Any]] = []
    for r in it.replay_intraday(agg, tf, step=step):
        if r["quality"] not in ("ok", "limited") and not all_quality:
            continue
        ts = r["ts"]
        rows.append({"sym": sym, "date": data.date_of(ts), "ts": ts, "tf": tf, "price": r["price"], "quality": r["quality"],
                     "session_complete": complete_by_day.get(_day(ts), False), "summary": r["summary"], "fwd": forward(agg, r["i"])})
    return rows


def coverage(days: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, Any]:
    by_date: Dict[str, Dict[str, int]] = {}
    by_sym: Dict[str, Dict[str, int]] = {}
    for sym, per in days.items():
        s = by_sym.setdefault(sym, {"days": 0, "complete_days": 0})
        for date, d in per.items():
            c = by_date.setdefault(date, {"symbols": 0, "complete": 0, "backfilled": 0})
            c["symbols"] += 1
            c["complete"] += 1 if d["complete"] else 0
            c["backfilled"] += 1 if d["backfilled"] else 0
            s["days"] += 1
            s["complete_days"] += 1 if d["complete"] else 0
    cd = [v["complete_days"] for v in by_sym.values()]
    return {"days": dict(sorted(by_date.items())), "symbols": len(by_sym), "sessions": len(by_date),
            "symbols_with_3plus_complete_days": sum(1 for v in cd if v >= 3), "max_complete_days": max(cd) if cd else 0, "by_symbol": dict(sorted(by_sym.items()))}


def coverage_markdown(cov: Dict[str, Any], rows_by_tf: Dict[int, int]) -> str:
    L_ = ["# Recorded intraday data - coverage", "", f"- sessions on disk: {cov['sessions']}, symbols: {cov['symbols']}, "
          f"symbols with >= 3 complete sessions: {cov['symbols_with_3plus_complete_days']}, best symbol: {cov['max_complete_days']} complete sessions",
          "- rows exported: " + (", ".join(f"{tf}m = {n}" for tf, n in sorted(rows_by_tf.items())) or "none"), "",
          "| date | symbols | complete from the open | backfilled |", "|---|---|---|---|"]
    for d, v in cov["days"].items():
        L_.append(f"| {d} | {v['symbols']} | {v['complete']} | {v['backfilled']} |")
    L_ += ["", "Intraday structure needs about two sessions of history per symbol before it is `ok`/`limited` (50 bars of 15 minutes); a gate backtest needs 20+ "
               "sessions before it can say anything (intraday_gate_backtest.py --min-sessions)."]
    return "\n".join(L_) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir")
    ap.add_argument("--out", default="")
    ap.add_argument("--tf", default="15,5")
    ap.add_argument("--step", type=int, default=2)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--all-quality", action="store_true")
    ap.add_argument("--include-partial", action="store_true")
    a = ap.parse_args(argv)
    tfs = [int(x) for x in a.tf.split(",") if x.strip()]
    bad = [t for t in tfs if t not in it.SUPPORTED_TF]
    if bad:
        print(f"unsupported --tf {bad}; supported: {list(it.SUPPORTED_TF)}", file=sys.stderr)
        return 2
    syms = [s.strip() for s in a.symbols.split(",") if s.strip()] or None
    stats: Dict[str, int] = {}
    days = data.load_dir(a.dir, syms, stats)
    out = Path(a.out) if a.out else Path(a.dir) / "intraday_out"
    out.mkdir(parents=True, exist_ok=True)
    counts: Dict[int, int] = {}
    for tf in tfs:
        n = 0
        with open(out / f"intraday_rows_{tf}m.jsonl", "w") as fh:
            for sym, per in sorted(days.items()):
                for row in symbol_rows(sym, per, tf, max(1, a.step), a.all_quality, a.include_partial):
                    fh.write(json.dumps(row, separators=(",", ":")) + "\n")
                    n += 1
        counts[tf] = n
    cov = coverage(days)
    cov["bad_files"] = stats.get("bad_files", 0)
    (out / "coverage.json").write_text(json.dumps(cov, indent=1))
    md = coverage_markdown(cov, counts)
    (out / "coverage.md").write_text(md)
    print(md)
    return 0 if days else 1


if __name__ == "__main__":
    sys.exit(main())
