#!/usr/bin/env python3
"""check_shared_copies.py - stop a fix landing in only one copy of a duplicated module.

WHY: every service is a separately deployed container with no shared import path, so modules such as
oracle_compat.py, kv_cache.py and rate_limiter.py exist as several copies. A fix applied to one copy and
missed in a twin is a silent bug (the oversized-weight rate-limit fix had to be applied by hand to several).

WHAT IT DOES (no imports of the modules, plain AST, so it needs no service dependencies):
  * IDENTICAL families (IDENTICAL below): every copy must be byte-for-byte the same. Strict, no baseline.
  * Other families: each top-level function / class / constant is hashed per copy. Differences that already exist
    are recorded in scripts/shared_copies_baseline.json (the "known differences": per-service variants on purpose).
    A NEW difference, or a known one that changed again, fails the check, so you decide: sync it, or accept it.

  python3 scripts/check_shared_copies.py                 # check (exit 1 on new drift)  <- run in CI / before a deploy
  python3 scripts/check_shared_copies.py --report        # list every difference, per family
  python3 scripts/check_shared_copies.py --update        # accept the current differences as the new baseline
  python3 scripts/check_shared_copies.py --sync oracle_compat   # copy the first copy over the others (IDENTICAL families only)
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "scripts" / "shared_copies_baseline.json"

# family name -> file name that is duplicated across services
FAMILIES = ["oracle_compat", "boot_forensics", "kv_cache", "rate_limiter", "circuit_breaker", "pipeline_status", "notifier"]
IDENTICAL = {"oracle_compat", "boot_forensics"}
SKIP_DIRS = {"tests", "__pycache__", "node_modules", ".git", "scripts"}


def find_copies(name: str) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT / "services"):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if f"{name}.py" in filenames:
            out.append(Path(dirpath) / f"{name}.py")
    return sorted(out)


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def symbols(path: Path) -> dict[str, str]:
    """top-level name -> short hash of its AST (comments / line numbers do not count)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    res: dict[str, str] = {}
    for node in tree.body:
        names = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        for n in names:
            res[n] = hashlib.sha1(ast.dump(node).encode()).hexdigest()[:8]
    return res


def differences(name: str) -> dict[str, dict[str, str]]:
    """symbol -> {copy path: hash or '-' when the copy lacks it}, only for symbols that are not the same everywhere."""
    copies = find_copies(name)
    per = {rel(c): symbols(c) for c in copies}
    allsyms = sorted({s for d in per.values() for s in d})
    diff = {}
    for s in allsyms:
        row = {p: d.get(s, "-") for p, d in per.items()}
        if len(set(row.values())) > 1:
            diff[s] = row
    return diff


def load_baseline() -> dict:
    try:
        return json.loads(BASELINE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def check_identical(name: str) -> list[Path]:
    copies = find_copies(name)
    digests = {hashlib.md5(c.read_bytes()).hexdigest() for c in copies}
    return copies if len(digests) > 1 else []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--sync", metavar="FAMILY")
    a = ap.parse_args()

    if a.sync:
        if a.sync not in IDENTICAL:
            print(f"--sync only works on identical families: {', '.join(sorted(IDENTICAL))}")
            return 2
        copies = find_copies(a.sync)
        for c in copies[1:]:
            shutil.copyfile(copies[0], c)
        print(f"{a.sync}: {len(copies)} copies now identical to {rel(copies[0])}")
        return 0

    current = {n: differences(n) for n in FAMILIES if n not in IDENTICAL}
    flat = {f"{n}::{s}": row for n, d in current.items() for s, row in d.items()}

    if a.report:
        for n in FAMILIES:
            if n in IDENTICAL:
                print(f"== {n}: {'identical' if not check_identical(n) else 'DIFFERENT'}")
            else:
                print(f"== {n}: {len(current[n])} symbol(s) differ")
                for s, row in current[n].items():
                    print(f"   {s}: " + ", ".join(f"{os.path.basename(os.path.dirname(p)) or p}={h}" for p, h in row.items()))
        return 0

    if a.update:
        BASELINE.write_text(json.dumps(flat, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"baseline written: {len(flat)} known differences in {rel(BASELINE)}")
        return 0

    problems = []
    for n in sorted(IDENTICAL):
        if check_identical(n):
            problems.append(f"  - {n}: the copies must be identical but are not  (fix one, then: scripts/check_shared_copies.py --sync {n})")
    base = load_baseline()
    for key, row in sorted(flat.items()):
        if base.get(key) != row:
            kind = "changed again" if key in base else "NEW"
            problems.append(f"  - {key}: {kind} difference between copies  ({', '.join(sorted(row))})")
    if problems:
        print("shared-copy drift:")
        print("\n".join(problems))
        print("Apply the fix to every copy, or - if the difference is meant - run: scripts/check_shared_copies.py --update")
        return 1
    print(f"OK - {len(FAMILIES)} families, no new drift ({len(base)} known differences are on record in {rel(BASELINE)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
