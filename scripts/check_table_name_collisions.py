#!/usr/bin/env python3
"""check_table_name_collisions.py - two services must not define the SAME table name with DIFFERENT columns.

On the Oracle VM every service connects to one schema, so a name used by two services is ONE table. create_all() skips a table that exists, so the second
service then queries columns that are not there (ORA-00904) and best-effort writers fail silently. group 374 hit this: training and real-trade both
defined `structure_observations`. Identical definitions (the intentional `stockky_shared_*` tables and the read-only `trade_credentials` mirror) pass.

  python3 scripts/check_table_name_collisions.py        exit 1 and list the clashes when any are found
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _repo_walk import walk_py  # noqa: E402  (never look inside a virtualenv / site-packages)

ROOT = Path(__file__).resolve().parent.parent / "services"
TABLE = re.compile(r'__tablename__\s*=\s*["\'](\w+)["\']')
COL = re.compile(r"\n\s+(\w+)\s*=\s*(?:Column|mapped_column)\(")


def find() -> dict:
    found: dict = {}
    for f in walk_py(ROOT):
        parts = f.relative_to(ROOT).parts
        if "tests" in parts or f.name.startswith("test_") or "node_modules" in parts or "venv" in parts or ".venv" in parts:
            continue
        txt = f.read_text(encoding="utf-8", errors="replace")
        for m in TABLE.finditer(txt):
            block = re.split(r"\nclass ", txt[m.end():])[0]
            found.setdefault(m.group(1), []).append((parts[0], str(f.relative_to(ROOT)), frozenset(COL.findall(block))))
    return found


def clashes() -> list:
    out = []
    for table, defs in sorted(find().items()):
        by_service = {}
        for svc, path, cols in defs:
            by_service.setdefault(svc, (path, cols))
        if len(by_service) < 2:
            continue
        shapes = {cols for _, cols in by_service.values()}
        if len(shapes) > 1:
            out.append((table, {svc: path for svc, (path, _) in by_service.items()}))
    return out


def main() -> int:
    bad = clashes()
    for table, where in bad:
        print(f"CLASH: table '{table}' is defined with different columns in: " + ", ".join(f"{s} ({p})" for s, p in where.items()))
    if bad:
        print("Rename one of them (all services share one Oracle schema).")
        return 1
    print("ok: no cross-service table-name clashes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
