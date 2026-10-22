#!/usr/bin/env python3
"""structure_backtest.py - WS-7: did the daily-structure rules help? Replays the engine over daily candles (no look-ahead) and prints rule effects,
a walk-forward test per rule and the calibration of the quality score as JSON.

  python3 scripts/structure_backtest.py DIR [--step 3] [--horizon 10] [--min-n 20] [--thresholds '{"chase_atr":1.5}']
                                        [--jobs 1] [--checkpoint FOLDER [--resume]]

group 491: --checkpoint saves every finished symbol (the slow part, about 2 hours for 400 symbols) so a killed / timed-out run continues with --resume
and computes only the missing symbols (a changed history file or changed --step / --entry / --cost-pct / --slip-pct is recomputed). The walk-forward and
calibration at the end are cheap and always recomputed from the saved samples, so changing --horizon / --min-n / --thresholds reuses the checkpoint.

DIR holds one JSON file per symbol: either the market-data /history reply ({"candles":[{date,open,high,low,close,volume},...]}) or a bare list of
such candles (save with: curl $MARKET_DATA_URL/history/SYM?period=5y > DIR/SYM.json). Needs only the repo (shared/market_structure); no network.
Everything printed is a hypothesis test on history, not a promise: read `verdict` / `insufficient_sample` before acting on an edge.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))

from market_structure.backtest import HORIZONS, build_samples, calibration, forward_outcome, rule_effects, walk_forward  # noqa: E402
from market_structure.candles import candles_to_bars  # noqa: E402
from market_structure.ms_types import O, finite  # noqa: E402
from market_structure.replay import replay  # noqa: E402

GRIDS = {"chase": [0.5, 1.0, 1.5, 2.0, 3.0, 4.0], "stretch": [2.0, 3.0, 4.0, 5.0], "room": [0.3, 0.5, 1.0, 1.5], "rr": [1.0, 1.2, 1.5, 2.0]}


def net_samples(bars, step: int = 3, cost_pct: float = 0.30, slip_pct: float = 0.10):
    """Same samples as build_samples but priced like a real trade (group 375, same convention as scripts/ms_ab_report.py): entry = OPEN of the next
    session + slippage, T+h counts the entry session as 1, the round-trip cost is subtracted from every return. Without this the walk-forward
    would tune thresholds on close-entry, cost-free returns that nobody can trade."""
    out = []
    for r in replay(bars, step=step):
        i = r["i"]
        if r["quality"] not in ("ok", "limited") or i + 1 >= len(bars) or not finite(bars[i + 1][O]) or bars[i + 1][O] <= 0:
            continue
        sm = r["summary"]
        entry = float(bars[i + 1][O]) * (1.0 + slip_pct / 100.0)
        oc = forward_outcome(entry, sm.get("ms_stop"), sm.get("ms_target"), bars[i + 1:])
        for h in HORIZONS:
            if oc[f"ret_{h}d"] is not None:
                oc[f"ret_{h}d"] = round(oc[f"ret_{h}d"] - cost_pct, 3)
        out.append({"i": i, "ts": r["ts"], "price": entry, "summary": sm, "outcome": oc})
    return out


BENCH_NAMES = ("^NSEI", "NIFTY", "NSEI", "NIFTY50")


def load_one(f: Path):
    """(SYMBOL, bars) for one history file, or None when it has fewer than 100 bars. Raises on an unreadable file."""
    doc = json.loads(f.read_text())
    candles = doc.get("candles") if isinstance(doc, dict) else doc
    bars = candles_to_bars(candles)
    return (f.stem.upper(), bars) if len(bars) >= 100 else None


def load(dir_: Path):
    for f in sorted(dir_.glob("*.json")):
        if f.stem.upper() in BENCH_NAMES:  # the benchmark tags regimes in ms_ab_report; it is never traded
            continue
        try:
            got = load_one(f)
            if got is not None:
                yield got
        except Exception as e:  # noqa: BLE001
            print(f"skip {f.name}: {e}", file=sys.stderr)


def _job(job):
    """One symbol (group 491, module level so a process pool can run it): {"sym", "samples", "err"}. samples is None when the file is skipped."""
    path, step, entry, cost_pct, slip_pct = job
    f = Path(path)
    try:
        got = load_one(f)
    except Exception as e:  # noqa: BLE001
        return {"sym": f.stem.upper(), "samples": None, "err": f"{type(e).__name__}: {e}"}
    if got is None:
        return {"sym": f.stem.upper(), "samples": None, "err": ""}
    sym, bars = got
    built = net_samples(bars, step, cost_pct, slip_pct) if entry == "next_open" else build_samples(bars, step=step)
    for s in built:
        s["symbol"] = sym
    return {"sym": sym, "samples": built, "err": ""}


def _stamp(path, step, entry, cost_pct, slip_pct):
    """What decides a saved symbol: the history file (size, mtime) and the settings that change the samples. Not horizon / min-n / thresholds."""
    from _progress import file_stamp
    return file_stamp(path, step, entry, float(cost_pct), float(slip_pct))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir")
    ap.add_argument("--step", type=int, default=3)
    ap.add_argument("--horizon", type=int, default=10, choices=(1, 5, 10, 20))
    ap.add_argument("--min-n", type=int, default=20)
    ap.add_argument("--thresholds", default="{}")
    ap.add_argument("--entry", choices=("next_open", "close"), default="next_open",
                    help="next_open (default): tradable, net of costs; close: the old signal-day-close, cost-free view")
    ap.add_argument("--cost-pct", type=float, default=0.30)
    ap.add_argument("--slip-pct", type=float, default=0.10)
    ap.add_argument("--jobs", type=int, default=1, help="parallel workers for the per-symbol replay (default 1: the old single process)")
    ap.add_argument("--checkpoint", default="", help="folder where each finished symbol is saved as the run goes (group 491); emptied at the start unless --resume")
    ap.add_argument("--resume", action="store_true", help="reuse the symbols already saved in --checkpoint (same file and settings), compute only the rest")
    a = ap.parse_args(argv)
    th = json.loads(a.thresholds)
    files = [f for f in sorted(Path(a.dir).glob("*.json")) if f.stem.upper() not in BENCH_NAMES]
    jobs = [(str(f), a.step, a.entry, a.cost_pct, a.slip_pct) for f in files]
    from _progress import run_jobs   # group427 progress line; group491 per-symbol checkpoint / resume (same helper as the other backtests)
    results = run_jobs(_job, jobs, a.jobs, "structure", ckpt=a.checkpoint, resume=a.resume, key=lambda j: Path(j[0]).stem.upper(),
                       stamp=lambda j: _stamp(j[0], *j[1:]))
    samples, symbols = [], 0
    for res in results:
        if res["err"]:
            print(f"skip {res['sym']}: {res['err']}", file=sys.stderr)
        if res["samples"] is None:
            continue
        symbols += 1
        samples.extend(res["samples"])
    done = [s for s in samples if s["outcome"].get(f"ret_{a.horizon}d") is not None]
    out = {"entry": a.entry, "cost_pct": a.cost_pct if a.entry == "next_open" else 0.0,
           "slip_pct": a.slip_pct if a.entry == "next_open" else 0.0, "symbols": symbols, "samples": len(samples), "with_outcome": len(done), "horizon": a.horizon,
           "rule_effects": rule_effects(done, a.horizon, th, a.min_n),
           "walk_forward": {r: walk_forward(done, r, g, horizon=a.horizon) for r, g in GRIDS.items()},
           "calibration": {k: calibration(done, k, horizon=a.horizon) for k in ("ms_tqs", "ms_loc_score", "ms_groups")}}
    print(json.dumps(out, indent=1))
    return 0 if symbols else 1


if __name__ == "__main__":
    sys.exit(main())
