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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))

from market_structure.backtest import build_samples, calibration, rule_effects, walk_forward  # noqa: E402
from market_structure.candles import candles_to_bars  # noqa: E402

GRIDS = {"chase": [0.5, 1.0, 1.5, 2.0, 3.0, 4.0], "stretch": [2.0, 3.0, 4.0, 5.0], "room": [0.3, 0.5, 1.0, 1.5], "rr": [1.0, 1.2, 1.5, 2.0]}


def load(dir_: Path):
    for f in sorted(dir_.glob("*.json")):
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
    a = ap.parse_args()
    th = json.loads(a.thresholds)
    samples, symbols = [], 0
    for sym, bars in load(Path(a.dir)):
        symbols += 1
        for s in build_samples(bars, step=a.step):
            s["symbol"] = sym
            samples.append(s)
    done = [s for s in samples if s["outcome"].get(f"ret_{a.horizon}d") is not None]
    out = {"symbols": symbols, "samples": len(samples), "with_outcome": len(done), "horizon": a.horizon,
           "rule_effects": rule_effects(done, a.horizon, th, a.min_n),
           "walk_forward": {r: walk_forward(done, r, g, horizon=a.horizon) for r, g in GRIDS.items()},
           "calibration": {k: calibration(done, k, horizon=a.horizon) for k in ("ms_tqs", "ms_loc_score")}}
    print(json.dumps(out, indent=1))
    return 0 if symbols else 1


if __name__ == "__main__":
    sys.exit(main())
