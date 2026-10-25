#!/usr/bin/env python3
"""swing_universe.py - E132: print the swing lane's universe from the history folder the replays validated (so the lane ranks the SAME stocks S2 tested).

  python3 scripts/swing_universe.py HIST_DIR [--max 450]       prints  SWING_UNIVERSE_SYMBOLS=A,B,C,...   (paste the line into .env, set SWING_MODE=shadow)

Benchmarks (^NSEI ...) are left out. Read-only, stdlib only. Exit 1 when the folder has no symbol files.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ms_ab_report import BENCH_NAMES  # noqa: E402


def symbols(folder: Path, cap: int = 450):
    return sorted({f.stem.upper() for f in Path(folder).glob("*.json") if f.stem.upper() not in BENCH_NAMES})[:cap]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir")
    ap.add_argument("--max", type=int, default=450)
    a = ap.parse_args(argv)
    s = symbols(Path(a.dir), a.max)
    if not s:
        print(f"NO SYMBOLS: {a.dir} has no symbol files", file=sys.stderr)
        return 1
    print("SWING_UNIVERSE_SYMBOLS=" + ",".join(s))
    print(f"# {len(s)} symbols; SWING_UNIVERSE_MAX must be at least {len(s)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
