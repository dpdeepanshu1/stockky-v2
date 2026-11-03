#!/usr/bin/env python3
"""Run Stockky daily-bar backtests counting only signals from the last N months."""
from __future__ import annotations
import argparse, sys
from datetime import date, timedelta
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("hist")
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--which", choices=("legacy", "entry", "both"), default="both")
    a, extra = ap.parse_known_args()
    extra = [x for x in extra if x != "--"]
    cutoff = (date.today() - timedelta(days=round(a.months * 30.44))).isoformat()
    scripts = Path(a.repo).resolve() / "scripts"
    sys.path[:0] = [str(scripts), str(scripts.parent / "shared")]
    print(f"[6m] signal days >= {cutoff}")
    rc = 0
    if a.which in ("legacy", "both"):
        import legacy_gate_backtest as m
        orig = m.rows_for
        m.rows_for = lambda *x, **k: [r for r in orig(*x, **k) if r["day"] >= cutoff]
        rc |= m.main([a.hist, "--out", str(Path(a.hist) / "legacy_6m_out"), "--jobs", "1", "--step", "1", *extra]) or 0
    if a.which in ("entry", "both"):
        import entry_rules_backtest as m
        orig = m.rows_for
        def _f(*x, **k):
            rows, grid = orig(*x, **k)
            return ([r for r in rows if r["day"] >= cutoff], {key: v for key, v in grid.items() if key[1] >= cutoff[:7]})
        m.rows_for = _f
        rc |= m.main([a.hist, "--out", str(Path(a.hist) / "entry_6m_out"), "--jobs", "1", "--step", "1", *extra]) or 0
    return rc


if __name__ == "__main__":
    sys.exit(main())
