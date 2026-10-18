#!/usr/bin/env python3
"""structure_backtest.py - WS-7: did the daily-structure rules help? Replays the engine over daily candles (no look-ahead) and prints rule effects,
a walk-forward test per rule and the calibration of the quality score as JSON.

  python3 scripts/structure_backtest.py DIR [--step 3] [--horizon 10] [--min-n 20] [--thresholds '{"chase_atr":1.5}']

DIR holds one JSON file per symbol: either the market-data /history reply ({"candles":[{date,open,high,low,close,volume},...]}) or a bare list of
such candles (save with: curl $MARKET_DATA_URL/history/SYM?period=5y > DIR/SYM.json). Needs only the repo (shared/market_structure); no network.
Everything printed is a hypothesis test on history, not a promise: read `verdict` / `insufficient_sample` before acting on an edge.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
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


def load(dir_: Path):
    for f in sorted(dir_.glob("*.json")):
        if f.stem.upper() in ("^NSEI", "NIFTY", "NSEI", "NIFTY50"):  # the benchmark tags regimes in ms_ab_report; it is never traded
            continue
        try:
            doc = json.loads(f.read_text())
            candles = doc.get("candles") if isinstance(doc, dict) else doc
            bars = candles_to_bars(candles)
            if len(bars) >= 100:
                yield f.stem.upper(), bars
        except Exception as e:  # noqa: BLE001
            print(f"skip {f.name}: {e}", file=sys.stderr)


def main() -> int:
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
    a = ap.parse_args()
    th = json.loads(a.thresholds)
    samples, symbols = [], 0
    t0 = time.monotonic()
    _total = sum(1 for f in Path(a.dir).glob("*.json") if f.stem.upper() not in ("^NSEI", "NIFTY", "NSEI", "NIFTY50"))
    print(f"[structure] starting about {_total} symbols (one at a time: this one is slow, a progress line follows every 5)", file=sys.stderr, flush=True)
    for sym, bars in load(Path(a.dir)):
        symbols += 1
        if symbols % 5 == 0:                       # group427: a progress line so a long run does not look hung
            print(f"[structure] {symbols}/{_total} symbols  elapsed {(time.monotonic() - t0) / 60:.1f} min", file=sys.stderr, flush=True)
        built = net_samples(bars, a.step, a.cost_pct, a.slip_pct) if a.entry == "next_open" else build_samples(bars, step=a.step)
        for s in built:
            s["symbol"] = sym
            samples.append(s)
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
