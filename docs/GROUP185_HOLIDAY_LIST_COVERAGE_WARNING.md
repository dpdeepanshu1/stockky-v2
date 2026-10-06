# Group 185 - warn at boot when the NSE holiday list has run out (api-gateway)

Cumulative on group 184. This is the "2027 holiday dates in December" item. Rebuild: `docker compose build api-gateway && docker compose up -d`.

## Why the dates themselves are NOT added
NSE has not published a 2027 list yet. It publishes each year's circular in December (the 2026 ones are dated 12 and 23
Dec 2025: NSE/FAOP/71777, NSE/CD/71962, NSE/DS/71968). The third-party calendars that already show 2027 are estimates and
disagree with each other (Muharram is 15 Jun on one and 16 Jun on another; one lists weekend dates only). A wrong date is
costly both ways: a fake holiday stops a trading day, a missing one trades into a closed market. So nothing was entered.

## What the risk is
Seven hand-kept copies of the holiday list hold 2026 only (`scripts/check_holiday_lists_sync.py` lists them; I ran it and
all seven agree today). From 1 Jan 2027 every holiday would count as a trading day, silently, until someone adds the new list.

## Change
- `nse_holidays.py::holiday_coverage_warning(today=None)`: returns a message when the set has no dates for the current year,
  or, from 15 Nov (`HOLIDAY_WARN_FROM`), none for next year; else None.
- `main.py` startup logs it as a WARNING (non-fatal). From 15 Nov 2026 every gateway boot says it, until the 2027 dates are in.
  The text names the seven-copy script.

## What to do in December
Take the dates from NSE's "Trading holidays for the calendar year 2027" circular, add them to all seven copies (the script's
docstring lists the files; the real-trade, position-stocks, market-data, analysis, decision and scheduler copies are named
`..._2026` and need a 2027 entry too), run `python3 scripts/check_holiday_lists_sync.py`, rebuild. The warning stops once
the gateway copy has 2027 dates.

## Related note (not changed)
NSE's 2026 circulars say Diwali Muhurat trading is on Sunday 8 Nov 2026 (timings to be announced). All copies treat
Sundays as closed, so that special session would be skipped. One broker page says Wed 21 Oct instead; the NSE circulars are
the authority. If you want to trade the Muhurat hour, say so and wait for the timing circular.

## Tests
New `tests/test_group185_holiday_coverage_warning.py` (6 cases: quiet before 15 Nov, warns for next year from 15 Nov, warns
for a year with no dates, quiet once entered, datetime reduced to the IST date, default never raises). No pytest in my
sandbox: the 6 cases ran directly and passed; the full api-gateway suite was NOT run.
On the VM: `cd services/api-gateway && python3 -m pytest tests/test_group185_holiday_coverage_warning.py tests/test_nse_holidays.py -q`
