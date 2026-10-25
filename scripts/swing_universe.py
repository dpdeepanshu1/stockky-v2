#!/usr/bin/env python3
"""swing_universe.py - E132: print the swing lane's universe from the history folder the replays validated (so the lane ranks the SAME stocks S2 tested).

  python3 scripts/swing_universe.py HIST_DIR [--max N]   prints  SWING_UNIVERSE_SYMBOLS=A,B,C,...  and  SWING_UNIVERSE_MAX=<count>   (paste BOTH lines into .env, set SWING_MODE=shadow)

E139: the default is now ALL symbols in the folder. It used to keep the first 450 ALPHABETICALLY, so on a 744-symbol history folder the lane would have ranked only A..K while the replay tested all 744. A --max below the folder size now prints a TRUNCATED warning.

Benchmarks (^NSEI ...) are left out. Read-only, stdlib only. Exit 1 when the folder has no symbol files.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ms_ab_report import BENCH_NAMES  # noqa: E402


def all_symbols(folder: Path):
    return sorted({f.stem.upper() for f in Path(folder).glob("*.json") if f.stem.upper() not in BENCH_NAMES})


def symbols(folder: Path, cap: int = 0):
    """Every symbol in the folder, or the first `cap` alphabetically when cap > 0."""
    s = all_symbols(folder)
    return s[:cap] if cap and cap > 0 else s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir")
    ap.add_argument("--max", type=int, default=0, help="0 (default) = every symbol in the folder")
    a = ap.parse_args(argv)
    full = all_symbols(Path(a.dir))
    if not full:
        print(f"NO SYMBOLS: {a.dir} has no symbol files", file=sys.stderr)
        return 1
    s = full[:a.max] if a.max and a.max > 0 else full
    print("SWING_UNIVERSE_SYMBOLS=" + ",".join(s))
    print(f"SWING_UNIVERSE_MAX={len(s)}")
    if len(s) < len(full):
        print(f"# TRUNCATED: the folder holds {len(full)} symbols, this kept the first {len(s)} alphabetically ({s[0]}..{s[-1]}); the replay tested all {len(full)}", file=sys.stderr)
    print(f"# {len(s)} symbols; paste both lines into .env (the lane caps at SWING_UNIVERSE_MAX, default 450, alphabetically)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
