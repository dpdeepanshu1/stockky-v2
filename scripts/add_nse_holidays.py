#!/usr/bin/env python3
"""
add_nse_holidays.py - adds one year's NSE trading holidays to all eight holiday-list copies in one step.

WHY: the holiday calendar lives in eight separately deployed files (see scripts/check_holiday_lists_sync.py). NSE
publishes next year's list by circular in December (the 2026 list: circulars of 23 Dec 2025). Until the dates are in
the files every day of that year counts as a trading day. Editing eight files by hand is how the 2026-09-14 incident
happened, so this script does it in one go, in each file's own format, and checks the result.

It never guesses: --circular (the NSE circular reference and date the list comes from) is required and is written
into every file next to the dates. Each file carries a "<year> PENDING" marker; the script replaces that marker with
the dates and puts a "<year+1> PENDING" marker back, so next December works the same way.

Usage (dry run is the default - it changes nothing):
    python3 scripts/add_nse_holidays.py --year 2027 --circular "NSE/CMTR/xxxxx dated 2026-12-DD" \\
        2027-01-26="Republic Day" 2027-03-22="Holi" ...
    python3 scripts/add_nse_holidays.py --year 2027 --circular "..." --from-file list.txt      # one "YYYY-MM-DD  Name" per line
    ... add --apply to write the files.

Rules: every date must be in --year; a Saturday or Sunday is skipped (the lists never held weekend dates - the
exchange is closed anyway); a date a file already has is left alone; if ANY file cannot be updated nothing is
written (all or nothing). After --apply the sync check (scripts/check_holiday_lists_sync.py) runs on the real repo.

Exit code 0 = done (or dry run fine / nothing to do), 1 = refused or sync check failed.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent


def _load_sync():
    spec = importlib.util.spec_from_file_location("check_holiday_lists_sync", HERE / "check_holiday_lists_sync.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SYNC = _load_sync()
# The eight copies, relative to the repo root (taken from the sync script so the two can never disagree).
REL_FILES = [str(p.relative_to(SYNC.REPO_ROOT)) for p in SYNC.FILES.values()]

_LABEL_BAD = re.compile(r"[\[\]{}\"'\r\n\\]")


def clean_label(label: str) -> str:
    """A label ends up in a code comment inside the list literal: brackets, quotes and newlines would break the
    sync script's bracket matching, so they are dropped."""
    label = _LABEL_BAD.sub("", re.sub(r"[\r\n]+", " ", (label or "").strip()))
    return re.sub(r"\s+", " ", label) or "NSE trading holiday"


def parse_entries(items: list[str], year: int) -> tuple[list[tuple[date, str]], list[str]]:
    """'YYYY-MM-DD=Label' or 'YYYY-MM-DD Label' -> sorted unique [(date, label)], plus a list of problems."""
    out: dict[date, str] = {}
    problems: list[str] = []
    for raw in items:
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\s*(?:[=:,]|\s)\s*(.*)$", raw) or re.match(r"^(\d{4}-\d{2}-\d{2})$", raw)
        if not m:
            problems.append(f"not a date line: {raw!r}")
            continue
        try:
            d = date.fromisoformat(m.group(1))
        except ValueError:
            problems.append(f"not a real date: {m.group(1)}")
            continue
        if d.year != year:
            problems.append(f"{d.isoformat()} is not in {year}")
            continue
        label = clean_label(m.group(2) if m.lastindex and m.lastindex >= 2 else "")
        out[d] = label
    return sorted(out.items()), problems


def _marker_block(lines: list[str], year: int) -> tuple[int, int] | None:
    """[start, end) line range of the '<year> PENDING' comment: the marker line and the comment lines right after it."""
    pat = re.compile(rf"^\s*#\s*{year}\s+PENDING\b")
    for i, ln in enumerate(lines):
        if pat.match(ln):
            j = i + 1
            while j < len(lines) and lines[j].lstrip().startswith("#"):
                j += 1
            return i, j
    return None


def _uses_date_tuples(block: str) -> bool:
    return "date(" in block and not SYNC._STR_DATE_RE.search(block)


def update_text(text: str, year: int, entries: list[tuple[date, str]], circular: str) -> tuple[str, list[str], list[str]]:
    """Returns (new_text, added_dates, skipped_notes). Raises ValueError when the file cannot be updated."""
    existing = SYNC._extract_dates(text)
    todo = [(d, lab) for d, lab in entries if d.isoformat() not in existing]
    skipped = [f"{d.isoformat()} already present" for d, _ in entries if d.isoformat() in existing]
    if not todo:
        return text, [], skipped
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    found = _marker_block(lines, year)
    if found is None:
        raise ValueError(f"no '# {year} PENDING' marker (the file was edited by hand or already has {year} dates)")
    start, end = found
    indent = re.match(r"^\s*", lines[start]).group(0)
    # which literal does the marker sit in, and in what form are its dates written?
    block = None
    for name in SYNC._LITERAL_NAMES:
        b = SYNC._extract_literal_block(text, name)
        if b is not None and lines[start].strip() in b:
            block = b
            break
    if block is None:
        raise ValueError("the PENDING marker is not inside a holiday literal")
    tuples = _uses_date_tuples(block)
    new: list[str] = [f"{indent}# {year} - NSE trading holidays, from circular: {clean_label(circular)}"]
    for d, lab in todo:
        if tuples:
            new.append(f"{indent}date({d.year}, {d.month}, {d.day}),   # {lab}")
        else:
            new.append(f'{indent}"{d.isoformat()}",  # {lab}')
    nxt = year + 1
    new.append(f"{indent}# {nxt} PENDING: NSE has not published the {nxt} trading holidays yet (expected in December {year}). "
               f"Not guessed. Add with scripts/add_nse_holidays.py --year {nxt}, then scripts/check_holiday_lists_sync.py.")
    new_text = nl.join(lines[:start] + new + lines[end:])
    got = SYNC._extract_dates(new_text)
    want = existing | {d.isoformat() for d, _ in todo}
    if got != want:
        raise ValueError("internal check failed: the updated list does not hold exactly the old dates plus the new ones")
    return new_text, [d.isoformat() for d, _ in todo], skipped


def run(root: Path, year: int, items: list[str], circular: str, apply: bool, out=print) -> int:
    if year < 2026:
        out(f"REFUSED: --year {year} is before 2026 (the lists start in 2026).")
        return 1
    if not (circular or "").strip():
        out("REFUSED: --circular is required (the NSE circular reference and date the list comes from). "
            "Nothing is guessed; the reference is written into every file.")
        return 1
    entries, problems = parse_entries(items, year)
    for p in problems:
        out(f"REFUSED: {p}")
    if problems:
        return 1
    weekend = [d for d, _ in entries if d.weekday() >= 5]
    for d in weekend:
        out(f"skipped {d.isoformat()} ({d.strftime('%A')}): the exchange is closed on weekends anyway, the lists never held weekend dates")
    entries = [(d, lab) for d, lab in entries if d.weekday() < 5]
    if not entries:
        out("REFUSED: no weekday dates were given.")
        return 1

    new_texts: dict[Path, str] = {}
    failed = False
    per_file_added: dict[str, list[str]] = {}
    for rel in REL_FILES:
        path = root / rel
        if not path.exists():
            out(f"MISSING FILE: {rel}")
            failed = True
            continue
        with open(path, "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
        try:
            new_text, added, skipped = update_text(text, year, entries, circular)
        except ValueError as e:
            out(f"CANNOT UPDATE {rel}: {e}")
            failed = True
            continue
        per_file_added[rel] = added
        if added:
            new_texts[path] = new_text
        out(f"{rel}: {'+' + str(len(added)) + ' date(s)' if added else 'nothing to add'}"
            + (f" ({'; '.join(skipped)})" if skipped else ""))
    if failed:
        out("\nNothing was written: at least one file could not be updated. Fix that file (or restore its marker) and run again.")
        return 1
    if not new_texts:
        out("\nNothing to do: every file already holds these dates.")
        return 0
    if not apply:
        out(f"\nDRY RUN - {len(new_texts)} file(s) would change. Add --apply to write them.")
        return 0
    # write every file via a temp file + replace, so a crash never leaves half a file
    for path, text in new_texts.items():
        tmp = path.with_name(path.name + ".part")
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    # all eight must agree now
    sets = {}
    for rel in REL_FILES:
        with open(root / rel, "r", encoding="utf-8", newline="") as fh:
            sets[rel] = SYNC._extract_dates(fh.read())
    union = set().union(*sets.values())
    drift = {rel: sorted(union - s) for rel, s in sets.items() if union - s}
    if drift:
        for rel, miss in drift.items():
            out(f"DRIFT in {rel}: missing {miss}")
        return 1
    out(f"\nWROTE {len(new_texts)} file(s); all {len(REL_FILES)} copies agree on {len(union)} dates.")
    if root.resolve() == REPO_ROOT.resolve():
        out("Now run: python3 scripts/check_holiday_lists_sync.py   then rebuild the services that carry a copy "
            "(api-gateway, market-data, position-stocks, real-trade, analysis-intelligence, decision-prediction, "
            "notification-scheduler, fno-options) on a weekday after 15:45 IST.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Add one year's NSE trading holidays to all eight holiday-list copies.")
    ap.add_argument("dates", nargs="*", help='YYYY-MM-DD="Holiday name" (quote the name)')
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--circular", default="", help='NSE circular reference and date, e.g. "NSE/CMTR/12345 dated 2026-12-20" (required)')
    ap.add_argument("--from-file", help='text file, one "YYYY-MM-DD  Holiday name" per line')
    ap.add_argument("--apply", action="store_true", help="write the files (default is a dry run)")
    a = ap.parse_args(argv)
    items = list(a.dates)
    if a.from_file:
        items += Path(a.from_file).read_text(encoding="utf-8").splitlines()
    return run(REPO_ROOT, a.year, items, a.circular, a.apply)


if __name__ == "__main__":
    sys.exit(main())
