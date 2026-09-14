#!/usr/bin/env python3
"""
check_holiday_lists_sync.py — verifies the four NSE-holiday-list copies in
this repo agree with each other.

WHY THIS EXISTS (2026-09-14 incident): there is no shared import path
between services (each is a separately-deployed container — see any
service's config.py isolation note), so the 2026 NSE/BSE trading-holiday
calendar is duplicated in FOUR places:
  - services/position-stocks-service/tz_utils.py   (_NSE_HOLIDAYS_2026)
  - services/real-trade-service/tz_utils.py         (_NSE_HOLIDAYS_2026)
  - services/api-gateway/nse_holidays.py            (_NSE_HOLIDAYS, has 2025+2026)
  - services/notification-scheduler-service/scheduler/run_once.py (HOLIDAYS_2026)

On 2026-09-14 (Ganesh Chaturthi) all four had drifted apart and every one
of them was ALSO missing that date, so the exchange-closed day ran full
scan/entry cycles and sent notifications as if the market were open. This
script parses all four with plain regex (deliberately NOT importing the
modules — run_once.py needs API_GATEWAY_URL etc. set just to import, which
defeats the point of a quick CI-friendly check) and fails loudly if they
don't all contain exactly the same 2026 date set.

Usage:
    python3 scripts/check_holiday_lists_sync.py

Exit code 0 = all four agree. Exit code 1 = drift detected (details printed).
Run this after editing any one of the four files.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FILES = {
    "position-stocks-service/tz_utils.py": (
        REPO_ROOT / "services/position-stocks-service/tz_utils.py"
    ),
    "real-trade-service/tz_utils.py": (
        REPO_ROOT / "services/real-trade-service/tz_utils.py"
    ),
    "api-gateway/nse_holidays.py": (
        REPO_ROOT / "services/api-gateway/nse_holidays.py"
    ),
    "notification-scheduler-service/scheduler/run_once.py": (
        REPO_ROOT / "services/notification-scheduler-service/scheduler/run_once.py"
    ),
}

# Matches "2026-01-26" (tz_utils.py / run_once.py string form) and
# "date(2026, 1, 26)" (nse_holidays.py form) — normalize both to YYYY-MM-DD.
_STR_DATE_RE = re.compile(r'"(2026-\d{2}-\d{2})"')
_TUPLE_DATE_RE = re.compile(r"date\(\s*2026\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)")

# Only the ACTUAL literal (list/set) assigned to one of these names counts —
# dates mentioned in prose comments/docstrings (e.g. explaining what the old,
# wrong date used to be) must NOT be picked up, or this check would report
# false drift forever. Each entry finds "<NAME> = [" or "<NAME> = {" and
# extracts only the text up to the matching close bracket.
_LITERAL_NAMES = ["_NSE_HOLIDAYS_2026", "_NSE_HOLIDAYS", "HOLIDAYS_2026"]


def _extract_literal_block(text: str, name: str) -> str | None:
    m = re.search(rf"\b{re.escape(name)}\s*(?::\s*[\w\[\], .]+)?\s*=\s*([\[{{])", text)
    if not m:
        return None
    open_ch = m.group(1)
    close_ch = "]" if open_ch == "[" else "}"
    start = m.end()  # just after the opening bracket
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
        i += 1
    return text[start:i]


def _extract_2026_dates(text: str) -> set[str]:
    dates: set[str] = set()
    for name in _LITERAL_NAMES:
        block = _extract_literal_block(text, name)
        if block is None:
            continue
        dates |= set(_STR_DATE_RE.findall(block))
        for month, day in _TUPLE_DATE_RE.findall(block):
            dates.add(f"2026-{int(month):02d}-{int(day):02d}")
    return dates


def main() -> int:
    per_file: dict[str, set[str]] = {}
    for label, path in FILES.items():
        if not path.exists():
            print(f"MISSING FILE: {label} not found at {path}")
            return 1
        per_file[label] = _extract_2026_dates(path.read_text())

    all_dates = set()
    for dates in per_file.values():
        all_dates |= dates

    ok = True
    for label, dates in per_file.items():
        missing = all_dates - dates
        if missing:
            ok = False
            print(f"DRIFT in {label}: missing {sorted(missing)}")

    if ok:
        print(f"OK — all {len(FILES)} holiday lists agree on {len(all_dates)} 2026 dates:")
        for d in sorted(all_dates):
            print(f"  {d}")
        return 0

    print(
        "\nFAILED — the four holiday-list copies have drifted apart. "
        "This is exactly the bug that caused cycles/notifications to run "
        "on Ganesh Chaturthi (2026-09-14). Bring every file above back in "
        "sync with the same 2026 date set before deploying."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
