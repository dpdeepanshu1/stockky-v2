# group142 (2026-10-04) - holiday-list sync script covers 2027+ and reminds you when a year runs out

Cumulative on group141. Tooling and tests only: no service code changed, nothing to rebuild.

## Why
All seven NSE-holiday copies (api-gateway, real-trade, position-stocks, market-data, technical, decision, notification-scheduler)
hold 2026 dates only. A date a list does not know counts as a normal trading day, so on 1 Jan 2027 every service quietly stops
skipping holidays unless 2027 dates are added to all seven. `scripts/check_holiday_lists_sync.py` could not help: it only looked
at 2026 dates, so 2027 dates added to one copy and forgotten in another would never be reported.

## Change (`scripts/check_holiday_lists_sync.py`)
- Compares every date from 2026 onward (string form `"2027-01-26"` and `date(2027, 1, 26)`), not just 2026. Years before 2026 are
  ignored on purpose (api-gateway keeps 2025 dates the others never had). The literal names are unchanged, so a 2027 date goes
  into the same set/list as the 2026 ones in each file.
- New `year_coverage_note()`: prints a NOTE (exit code stays 0) when no copy has dates for the current year, or, from 1 October,
  when none has next year's. Run today it prints: "no 2027 holiday dates yet ... add it to all 7 files" - that is the reminder.
- The OK line now shows the years: "all 7 holiday lists agree on 16 dates (2026)".
- Old helper name `_extract_2026_dates` kept as an alias.

## When 2027 dates are published (NSE usually in December)
Add them to the existing set in each of the seven files (see the list at the top of the script), then:
    python3 scripts/check_holiday_lists_sync.py
It fails with the file name and the missing dates if any copy differs; the note disappears once next year is present.
I did NOT add 2027 dates: I have no official 2027 calendar and a wrong date is worse than a missing one.

## Tests (`market-data-service/tests/test_nse_holiday_awareness.py`, +10)
Extraction of 2026/2027 string and date forms (2025 ignored, dates in comments ignored), old name alias, drift in a 2027 date
reported, agreeing multi-year lists pass (OK line shows "(2026, 2027)"), and five year-coverage cases (mid-year quiet, October
reminder, quiet once next year present, current-year-empty warning, September quiet). 28 pass here under a stand-in runner
(no pytest in the sandbox); on the VM: `bash run_tests.sh` in market-data-service.
