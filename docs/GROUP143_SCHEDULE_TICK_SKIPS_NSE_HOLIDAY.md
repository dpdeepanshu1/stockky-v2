# group143 (2026-10-04) - real-trade-service schedule tick skips an NSE weekday holiday

Cumulative on group142. Application code changed in real-trade-service only: `docker compose up -d --build real-trade-service`
(use your compose name).

## Problem
Last remaining weekday-only market check found by a repo-wide `weekday()` sweep (market-data main, api-gateway 3054/3067,
surprise_scanner, data_feed were already holiday-aware). `execution/auto_pilot.py` `_schedule_tick` gated only on
`is_ist_weekday()` (documented as "no exchange-holiday awareness"). Inside the tick, pre-pick and the eDIS morning check are
deliberately "market need not be open" jobs, so on Fri 2026-10-02 / Tue 2026-10-20 they ran and sent a picks / eDIS
notification for a day with no session. Enter-at-open, EOD square-off and EOD signal scan already require `is_market_open_ist()`
(holiday-aware) and were not affected.

## Change (`execution/auto_pilot.py`)
- New `_is_nse_holiday_today()` (uses `tz_utils.is_nse_holiday(ist_now())`; any error -> False).
- `_schedule_tick` returns early on a holiday, after the weekday check. A failed lookup keeps the old weekday-only behaviour.
- `tz_utils.is_ist_weekday` unchanged on purpose (its test pins "does not consult the holiday list").

## Tests (`tests/test_auto_pilot_orchestration.py`)
New `TestScheduleTickHoliday` (3): holiday skips, real calendar (2 Oct True, 1 Oct False), lookup failure False.
`test_schedule_tick_delegates_to_thread_on_weekday` now also patches `_is_nse_holiday_today` to False so it does not depend on
the run date. pytest is not available here: I ran the real `_schedule_tick` / helper source against the real `tz_utils`:
2 Oct skipped, 1 Oct ran, 20 Oct skipped. On the VM: `bash run_tests.sh` in real-trade-service.

## Not done
Same as group142: item 6 (needs yf_report.txt), item 9 remainder and item 12 (your decisions), item 25 (your review of the
regime constants), 2027 holiday dates (when NSE publishes).
