#!/usr/bin/env python3
"""sync_market_structure.py - vendor shared/market_structure/ into every service as `stockky_ms/`.

WHY: each service is its own Docker image with no shared import path (the images do not ship `shared/`), so the engine is copied
into every service that uses it. `shared/market_structure/` is the ONLY place to edit; the copies are generated.

  python3 scripts/sync_market_structure.py           # write / refresh the copies
  python3 scripts/sync_market_structure.py --check   # exit 1 if any copy differs from shared/ (run in CI / before a deploy)

The engine uses relative imports only, so the package name differs per location without any source rewrite. tests/ is not vendored.
Each copy carries a MANIFEST.sha256 (hash per file) so a hand-edit of a copy is caught even when shared/ did not change.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "shared" / "market_structure"
PKG = "stockky_ms"
# service dirs (relative to services/) that receive a copy at <service>/stockky_ms
TARGETS = ("analysis-intelligence-service", "api-gateway", "position-stocks-service", "decision-prediction-service", "real-trade-service")
MANIFEST = "MANIFEST.sha256"


def _files(d: Path) -> dict:
    return {p.name: p for p in sorted(d.glob("*.py"))}


def _digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _manifest(files: dict) -> str:
    return "".join(f"{_digest(p)}  {n}\n" for n, p in files.items())


def sync(write: bool) -> list:
    problems = []
    src = _files(SRC)
    if not src:
        return [f"source package empty: {SRC}"]
    want = _manifest(src)
    for svc in TARGETS:
        dst = ROOT / "services" / svc / PKG
        if not (ROOT / "services" / svc).is_dir():
            problems.append(f"{svc}: service dir missing")
            continue
        have = _files(dst) if dst.is_dir() else {}
        diff = [n for n in src if n not in have or _digest(have[n]) != _digest(src[n])] + [n for n in have if n not in src]
        mf = dst / MANIFEST
        if not mf.exists() or mf.read_text() != want:
            diff.append(MANIFEST)
        if not diff:
            continue
        if write:
            if dst.is_dir():
                shutil.rmtree(dst)
            dst.mkdir(parents=True)
            for n, p in src.items():
                shutil.copyfile(p, dst / n)
            (dst / MANIFEST).write_text(want)
            print(f"synced {svc}/{PKG} ({len(src)} files)")
        else:
            problems.append(f"{svc}/{PKG}: out of sync ({', '.join(sorted(set(diff)))})")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="do not write; exit 1 when any copy differs")
    a = ap.parse_args()
    problems = sync(write=not a.check)
    for p in problems:
        print("DRIFT:", p)
    if a.check and not problems:
        print(f"ok: {len(TARGETS)} copies of {PKG} match shared/market_structure")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
