#!/usr/bin/env python3
"""Fail when a file still holds git merge-conflict markers (group 341c).

Why: the 2026-10-13 VM log shows position-stocks-service in a restart loop for the whole session
(`/app/orders/reconcile.py", line 1032: <<<<<<< HEAD -> IndentationError`). A half-resolved merge reached the VM, the
container crashed on import, and nothing in the repo noticed. This check (CI + `python3 scripts/check_merge_conflicts.py`
before zipping / pulling to the VM) stops that.

A file is flagged when it has a `<<<<<<< ` or `>>>>>>> ` line (7 chars + a space or end of line) at the start of a line
(leading blanks allowed - the log's marker sat inside an indented block), or a `=======` line that has a start marker above it.
A lone `=======` is NOT flagged: Markdown headings and the underlines in test docstrings use it.

  python3 scripts/check_merge_conflicts.py            # whole repo, exit 1 + file:line list when something is found
  python3 scripts/check_merge_conflicts.py PATH ...   # only these files / folders
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _repo_walk  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEXT_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".sh", ".md", ".txt", ".css",
    ".html", ".env", ".example", ".recommended", ".sql", ".conf", ".patch",
}
TEXT_NAMES = {"Dockerfile", ".gitignore", ".dockerignore", ".env", "Makefile"}
START = re.compile(r"^[ \t]*<{7}(?: |$)")
END = re.compile(r"^[ \t]*>{7}(?: |$)")
MID = re.compile(r"^[ \t]*={7}[ \t]*$")
# .patch / .diff files legitimately quote conflict text; the archive holds old session notes
SKIP_DIRS = {"archive", "oracle_wallet"}


def _is_text_candidate(p: Path) -> bool:
    return p.suffix in TEXT_SUFFIXES or p.name in TEXT_NAMES or p.name.startswith(".env")


def scan_text(text: str) -> list[tuple[int, str]]:
    """(line number, kind) for each marker. `=======` counts only after an unclosed start marker."""
    hits: list[tuple[int, str]] = []
    open_block = False
    for n, line in enumerate(text.splitlines(), 1):
        if START.match(line):
            open_block = True
            hits.append((n, "<<<<<<<"))
        elif END.match(line):
            open_block = False
            hits.append((n, ">>>>>>>"))
        elif open_block and MID.match(line):
            hits.append((n, "======="))
    return hits


def iter_files(paths: list[Path]):
    for base in paths:
        if base.is_file():
            yield base
            continue
        for dirpath, dirnames, filenames in __import__("os").walk(base):
            here = Path(dirpath)
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not _repo_walk.is_vendored_dir(here / d))
            for f in sorted(filenames):
                p = here / f
                if _is_text_candidate(p) and p.suffix != ".patch":
                    yield p


def main(argv: list[str]) -> int:
    targets = [Path(a).resolve() for a in argv] or [ROOT]
    bad = 0
    for p in iter_files(targets):
        try:
            text = p.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue  # binary / unreadable: not a source file
        for n, kind in scan_text(text):
            try:
                shown = p.relative_to(ROOT)
            except ValueError:
                shown = p
            print(f"CONFLICT MARKER {kind}  {shown}:{n}")
            bad += 1
    if bad:
        print(f"\n{bad} merge-conflict marker line(s) found - resolve them before building or deploying.")
        return 1
    print("No merge-conflict markers.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
