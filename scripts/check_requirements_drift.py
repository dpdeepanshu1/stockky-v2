#!/usr/bin/env python3
"""check_requirements_drift.py - F33 part 2 (2026-10-23): keep the per-sub-service requirements files from drifting away from the
parent service's requirements.txt.

  python3 scripts/check_requirements_drift.py            # CI: fail on drift that is not in the accepted baseline
  python3 scripts/check_requirements_drift.py --list     # print every current difference

analysis-intelligence-service, decision-prediction-service and notification-scheduler-service each have a parent requirements.txt (what
docker-compose builds) and one per sub-app (standalone builds). A package that is in a sub file but missing from the parent, or pinned
differently, means "works standalone, breaks in the parent image" (or the reverse). Differences that exist TODAY are listed in BASELINE
below on purpose (the parent file wins; a sub-app file is aligned to it, never the other way round). A NEW difference, or a baseline entry that no
longer exists (fixed - delete the line), fails the check. Stdlib only, read-only; changes no file.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
PARENTS = ("analysis-intelligence-service", "decision-prediction-service", "notification-scheduler-service")

# (parent, sub-folder, file, package, kind) - the differences accepted on 2026-10-23. kind: "spec" = pinned differently, "missing" = absent in parent.
BASELINE = {
    # E46 (2026-10-23): the 8 pin differences were fixed by moving every sub-app file to the parent's pin (compose builds the parent, so
    # that is what runs and what the tests ran against). Only the two optional ML-extra packages remain, on purpose.
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


# E74: the same check ACROSS services (each services/<name>/requirements.txt against the others). The 22 files are deliberately NOT merged
# into one: real-trade-service / kronos / timesfm pin an older fastapi + pydantic stack, kronos needs torch, and the repo cannot run the
# service suites here to prove a bump safe. What is guarded instead: the differences that exist today are frozen in
# scripts/requirements_cross_baseline.json; a NEW package that drifts, or a pin that moves, fails CI until the baseline is regenerated on
# purpose (`--write-baseline`) in the same change that moves the pin.
CROSS_BASELINE = ROOT / "scripts" / "requirements_cross_baseline.json"


def cross_differences(root: Path = ROOT) -> Dict[str, Dict[str, str]]:
    """{package: {service: spec}} for every package whose spec is not identical in all the services that list it."""
    by: Dict[str, Dict[str, str]] = {}
    for f in sorted((root / "services").glob("*/requirements.txt")):
        for pkg, spec in parse(f).items():
            by.setdefault(pkg, {})[f.parent.name] = spec
    return {pkg: d for pkg, d in sorted(by.items()) if len(set(d.values())) > 1}


def cross_check(root: Path = ROOT, baseline_path: Path = CROSS_BASELINE) -> List[str]:
    """Problems: drift that is not in the frozen baseline, or a baseline entry that changed / was fixed."""
    cur = cross_differences(root)
    try:
        base = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        base = {}
    out: List[str] = []
    for pkg in sorted(set(cur) | set(base)):
        if cur.get(pkg) == base.get(pkg):
            continue
        if pkg not in base:
            out.append(f"NEW cross-service drift: {pkg} {cur[pkg]}")
        elif pkg not in cur:
            out.append(f"FIXED cross-service drift, regenerate the baseline (--write-baseline): {pkg}")
        else:
            out.append(f"CHANGED cross-service pins for {pkg}: baseline {base[pkg]} now {cur[pkg]} (--write-baseline if intended)")
    return out


def main(argv: List[str]) -> int:
    if "--write-baseline" in argv:
        CROSS_BASELINE.write_text(json.dumps(cross_differences(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {CROSS_BASELINE.name}: {len(cross_differences())} packages")
        return 0
    if "--list" in argv:
        for c in differences():
            mark = "accepted" if c[:5] in BASELINE else "NEW"
            print(f"[{mark}] {c[0]}/{c[1]}/{c[2]}: {c[3]} - {c[5]}")
        return 0
    new, stale = check()
    cross = cross_check()
    for line in new + stale + cross:
        print(line)
    if new or stale or cross:
        return 1
    print(f"requirements drift: ok ({len(BASELINE)} accepted differences, no new ones; cross-service baseline unchanged)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
