#!/usr/bin/env python3
"""check_stale_files.py - find files git still tracks that the latest release no longer contains.

Why: each release arrives as a zip that is unpacked over the git checkout. Unpacking adds and overwrites files but never
deletes, so a file removed in a release (for example frontend/src/components/app/TabButton.tsx, removed in E70) stays in git
for ever. Tests that assert "this file is gone" then fail in CI, and scanners such as scripts/gen_env_reference.py read the
leftover code and disagree with the committed docs. Everything passes on the zip itself, which is why it looks like a zip bug.

  python3 scripts/check_stale_files.py            # list tracked files that are not in docs/FILE_MANIFEST.txt, with the git rm lines
  python3 scripts/check_stale_files.py --write    # (release step) rewrite docs/FILE_MANIFEST.txt from the files on disk

Run the first form right after unpacking a release and before committing. It changes nothing; it prints the commands.
Files you added yourself after the release will also be listed: read the list before running the `git rm` lines.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "docs" / "FILE_MANIFEST.txt"
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", "dist", "htmlcov"}


def disk_files(root: Path = ROOT) -> list[str]:
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts) or p.suffix == ".pyc":
            continue
        out.append(rel.as_posix())
    return out


def tracked_files(root: Path = ROOT) -> list[str] | None:
    try:
        r = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return [x for x in r.stdout.decode("utf-8", "replace").split("\0") if x]


def stale(tracked: list[str], manifest: set[str]) -> list[str]:
    return sorted(f for f in tracked if f not in manifest)


def main(argv: list[str]) -> int:
    if "--write" in argv:
        files = disk_files()
        if "docs/FILE_MANIFEST.txt" not in files:
            files.append("docs/FILE_MANIFEST.txt")
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text("\n".join(sorted(files)) + "\n", encoding="utf-8")
        print(f"wrote {MANIFEST.relative_to(ROOT)}: {len(files)} files")
        return 0
    if not MANIFEST.exists():
        print("docs/FILE_MANIFEST.txt is missing - nothing to compare with (a release step writes it: --write)")
        return 2
    tracked = tracked_files()
    if tracked is None:
        print("this folder is not a git checkout (or git is not installed); run this inside your repository")
        return 2
    known = {l.strip() for l in MANIFEST.read_text(encoding="utf-8").splitlines() if l.strip()}
    gone = stale(tracked, known)
    if not gone:
        print(f"no stale files: all {len(tracked)} tracked files are in this release")
        return 0
    print(f"{len(gone)} tracked file(s) are not in this release (removed by it, or added by you since). Check, then run:\n")
    for f in gone:
        print(f"git rm -f -- '{f}'")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
