#!/usr/bin/env python3
"""check_requirements_drift.py - F33 part 2 (2026-10-23): keep the per-sub-service requirements files from drifting away from the
parent service's requirements.txt.

  python3 scripts/check_requirements_drift.py            # CI: fail on drift that is not in the accepted baseline
  python3 scripts/check_requirements_drift.py --list     # print every current difference

analysis-intelligence-service, decision-prediction-service and notification-scheduler-service each have a parent requirements.txt (what
docker-compose builds) and one per sub-app (standalone builds). A package that is in a sub file but missing from the parent, or pinned
differently, means "works standalone, breaks in the parent image" (or the reverse). Differences that exist TODAY are listed in BASELINE
below on purpose: changing a pin in a live image is a decision for the owner, not for a lint. A NEW difference, or a baseline entry that no
longer exists (fixed - delete the line), fails the check. Stdlib only, read-only; changes no file.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
PARENTS = ("analysis-intelligence-service", "decision-prediction-service", "notification-scheduler-service")

# (parent, sub-folder, file, package, kind) - the differences accepted on 2026-10-23. kind: "spec" = pinned differently, "missing" = absent in parent.
BASELINE = {
    ("analysis-intelligence-service", "event", "requirements.txt", "yfinance", "spec"),
    ("analysis-intelligence-service", "fundamental", "requirements.txt", "httpx", "spec"),
    ("analysis-intelligence-service", "news", "requirements.txt", "httpx", "spec"),
    ("analysis-intelligence-service", "technical", "requirements.txt", "httpx", "spec"),
    ("decision-prediction-service", "decision", "requirements.txt", "fastapi", "spec"),
    ("decision-prediction-service", "decision", "requirements.txt", "uvicorn", "spec"),
    ("decision-prediction-service", "decision", "requirements.txt", "httpx", "spec"),
    ("decision-prediction-service", "decision", "requirements.txt", "pydantic", "spec"),
    ("decision-prediction-service", "prediction", "requirements-ml-extra.txt", "imbalanced-learn", "missing"),
    ("decision-prediction-service", "prediction", "requirements-ml-extra.txt", "shap", "missing"),
}

_LINE = re.compile(r"^([A-Za-z0-9_.\-]+)(\[[^\]]*\])?\s*(.*)$")


def parse(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        ln = raw.split("#", 1)[0].strip()
        if not ln or ln.startswith("-"):
            continue
        m = _LINE.match(ln)
        if m:
            out[m.group(1).lower().replace("_", "-")] = m.group(3).replace(" ", "")
    return out


def differences(root: Path = ROOT) -> List[Tuple[str, str, str, str, str, str]]:
    """(parent, sub, file, package, kind, detail) for every current difference."""
    found = []
    for parent in PARENTS:
        pfile = root / "services" / parent / "requirements.txt"
        if not pfile.is_file():
            continue
        pm = parse(pfile)
        for sub in sorted((root / "services" / parent).glob("*/requirements*.txt")):
            for pkg, spec in parse(sub).items():
                if pkg not in pm:
                    found.append((parent, sub.parent.name, sub.name, pkg, "missing", f"{pkg}{spec} not in the parent"))
                elif pm[pkg] != spec:
                    found.append((parent, sub.parent.name, sub.name, pkg, "spec", f"sub {spec or '(unpinned)'} vs parent {pm[pkg] or '(unpinned)'}"))
    return found


def check(root: Path = ROOT) -> Tuple[List[str], List[str]]:
    """(new_problems, stale_baseline_lines)."""
    cur = differences(root)
    keys = {c[:5] for c in cur}
    new = [f"NEW {c[0]}/{c[1]}/{c[2]}: {c[3]} - {c[5]}" for c in cur if c[:5] not in BASELINE]
    stale = [f"STALE baseline (fixed, remove it): {'/'.join(b[:3])} {b[3]} ({b[4]})" for b in sorted(BASELINE) if b not in keys]
    return new, stale


def main(argv: List[str]) -> int:
    if "--list" in argv:
        for c in differences():
            mark = "accepted" if c[:5] in BASELINE else "NEW"
            print(f"[{mark}] {c[0]}/{c[1]}/{c[2]}: {c[3]} - {c[5]}")
        return 0
    new, stale = check()
    for line in new + stale:
        print(line)
    if new or stale:
        return 1
    print(f"requirements drift: ok ({len(BASELINE)} accepted differences, no new ones)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
