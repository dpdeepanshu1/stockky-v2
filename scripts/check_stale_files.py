#!/usr/bin/env python3
"""check_stale_files.py - find files git still tracks that the latest release no longer contains.

Why: each release arrives as a zip that is unpacked over the git checkout. Unpacking adds and overwrites files but never
deletes, so a file removed in a release (for example frontend/src/components/app/TabButton.tsx, removed in E70) stays in git
for ever. Tests that assert "this file is gone" then fail in CI, and scanners such as scripts/gen_env_reference.py read the
leftover code and disagree with the committed docs. Everything passes on the zip itself, which is why it looks like a zip bug.

  python3 scripts/check_stale_files.py            # list tracked files that are not in docs/FILE_MANIFEST.txt, with the git rm lines
  python3 scripts/check_stale_files.py --write    # (release step) rewrite docs/FILE_MANIFEST.txt from the files on disk
  python3 scripts/check_stale_files.py --verify   # (release step, E285 round 96) fail when the manifest and the files on disk differ, in either direction
                                                  # (round 122: files .gitignore covers and the manifest does not list, such as .venv-test/, are left out;
                                                  #  round 124: inside a git checkout only the files git TRACKS are compared with the manifest, and the
                                                  #  untracked local leftovers (bars/, hist400/, *.log ...) are summarised in one line, not listed)

WHY --verify: in round 95 seven live files (shared_rate_window.py, liveStateView.ts and their tests) were on disk but missing from the manifest, so the first form told the VM to `git rm` them.
Deleting those would have broken the shared order window. A release is not done until `--verify` says the manifest matches the disk.

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


def git_ignored(paths: list[str], root: Path = ROOT) -> set[str]:
    """E285 round 122: the subset of `paths` that .gitignore covers (empty outside a git checkout or when git is missing)."""
    if not paths:
        return set()
    try:
        r = subprocess.run(["git", "-C", str(root), "check-ignore", "--no-index", "-z", "--stdin"],
                           input="\0".join(paths).encode("utf-8"), capture_output=True)
    except OSError:
        return set()
    if r.returncode not in (0, 1):                      # 128 = not a git repository
        return set()
    return {x for x in r.stdout.decode("utf-8", "replace").split("\0") if x}


def tracked_files(root: Path = ROOT) -> list[str] | None:
    try:
        r = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return [x for x in r.stdout.decode("utf-8", "replace").split("\0") if x]


def untracked_summary(paths: list[str], groups: int = 8) -> str:
    """E285 round 124: ONE line for the untracked local leftovers (a top-level folder with a count, or the file name), biggest first. Never one line per file."""
    by: dict[str, int] = {}
    for f in paths:
        head = f.split("/", 1)[0] + ("/" if "/" in f else "")
        by[head] = by.get(head, 0) + 1
    top = sorted(by.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = ", ".join(f"{k} ({n})" if k.endswith("/") else k for k, n in top[:groups])
    more = f", +{len(top) - groups} more" if len(top) > groups else ""
    return (f"note: {len(paths)} untracked local file(s) are not in the manifest and were not checked (git never tracked them, so there is nothing to remove from git): {shown}{more}. "
            f"List them with: git clean -nd")


def stale(tracked: list[str], manifest: set[str]) -> list[str]:
    return sorted(f for f in tracked if f not in manifest)


def main(argv: list[str]) -> int:
    if "--verify" in argv:
        if not MANIFEST.exists():
            print("docs/FILE_MANIFEST.txt is missing")
            return 2
        on_disk = set(disk_files())
        on_disk.add("docs/FILE_MANIFEST.txt")
        listed = {l.strip() for l in MANIFEST.read_text(encoding="utf-8").splitlines() if l.strip()}
        # E285 round 124: inside a git checkout the VM also holds UNTRACKED leftovers that .gitignore does not cover (bars/, hist400/, calibration_out/, kronos*.log,
        # rt_*.json, your own scripts). The round 122 filter only dropped ignored files, so --verify still printed ~10,000 "git rm" lines. Git never tracked them, so there is
        # nothing to remove from git: compare the manifest only with what git tracks (plus any file the manifest lists) and summarise the rest in ONE line.
        leftovers: list[str] = []
        tracked = tracked_files()
        if tracked:                                         # a checkout with files in the index; an empty or missing index falls back to the disk-only rules below
            tracked_here = {f for f in tracked if f in on_disk}
            ign = git_ignored(sorted(on_disk - tracked_here - listed))        # .gitignore already covers these (.venv-test/ ...): not worth a mention
            leftovers = sorted(on_disk - tracked_here - listed - ign)
            on_disk = tracked_here | (on_disk & listed)
        else:
            # E285 round 122: files .gitignore covers (.venv-test/, stockky_test_report_*/, saved run output) are not part of any release. A file the manifest lists is still checked.
            on_disk -= {f for f in git_ignored(sorted(on_disk - listed)) if f not in listed}
        extra, missing = sorted(on_disk - listed), sorted(listed - on_disk)
        if not extra and not missing:
            print(f"manifest matches the disk: {len(listed)} files")
            if leftovers:
                print(untracked_summary(leftovers))
            return 0
        for f in extra:
            print(f"on disk but not in the manifest (the VM would be told to git rm it): {f}")
        for f in missing:
            print(f"in the manifest but not on disk: {f}")
        if leftovers:
            print(untracked_summary(leftovers))
        print("fix: python3 scripts/check_stale_files.py --write")
        return 1
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
