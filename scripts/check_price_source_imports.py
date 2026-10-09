#!/usr/bin/env python3
"""group276 (Phase A contract): direct yfinance use is only allowed where it is listed in
scripts/price_source_allowlist.txt. Prices and candles for every service must come from market-data-service
(provider order dhan -> angelone -> yfinance, QUOTE_PROVIDER_ORDER / HISTORY_PROVIDER_ORDER).
Run from the repo root:  python3 scripts/check_price_source_imports.py   (exit 1 on a new offender)
Add --list to print current offenders; migrate a file, then delete its line from the allowlist."""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAT = re.compile(r"^\s*(import yfinance|from yfinance)", re.M)
ALLOW_FILE = ROOT / "scripts" / "price_source_allowlist.txt"


def offenders() -> list[str]:
    out = []
    for p in sorted((ROOT / "services").rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if "/tests/" in rel or rel.endswith("_test.py") or "/test_" in rel:
            continue
        try:
            if PAT.search(p.read_text(encoding="utf-8", errors="ignore")):
                out.append(rel)
        except OSError:
            pass
    return out


def main() -> int:
    allowed = {l.strip() for l in ALLOW_FILE.read_text().splitlines() if l.strip() and not l.startswith("#")} \
        if ALLOW_FILE.exists() else set()
    found = offenders()
    if "--list" in sys.argv:
        print("\n".join(found))
        return 0
    new = [f for f in found if f not in allowed]
    stale = sorted(allowed - set(found))
    for f in new:
        print(f"NEW direct yfinance import (use market-data-service): {f}")
    for f in stale:
        print(f"note: allowlist entry no longer needed (migrated): {f}")
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
