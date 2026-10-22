# E41 - entry time cut-offs on the Real Money Calibration page (2026-10-23)

The page had no time-of-day field. New switch kind `time` (HH:MM IST, 24-hour) in `control/kit.py` (both copies, identical): validated, written as `HH:MM`, and a LATER cut-off counts as LOOSENING (so it needs the usual confirm). The page shows a time picker + Apply.

| Service | On the page now (group "Entry time limits") |
| --- | --- |
| real-trade-service | `ENTRY_LAST_TIME_IST` (14:00), `CALENDAR_EXPIRY_ENTRY_CUTOFF_IST` (14:30), `CALENDAR_PRE_HOLIDAY_ENTRY_CUTOFF_IST` (14:30) |
| position-stocks-service | `SCALP_ENTRY_CUTOFF_IST` (14:00), `CALENDAR_EXPIRY_ENTRY_CUTOFF_IST` (14:30), `CALENDAR_PRE_HOLIDAY_ENTRY_CUTOFF_IST` (14:30) |

All are read at call time (`scripts/check_control_catalog.py` passes); `shipped` equals the config default (tested).
**Not on the page:** `CALENDAR_FRIDAY_ENTRY_CUTOFF_IST` (its default is blank = no cut-off, and the page needs a real time). To switch a cut-off off from the page use 23:59.
Tests: `scripts/test_control_kit.py` (+2), 20 pass in the sandbox. Frontend (`SwitchesPanel.tsx`, `calibrationApi.ts`) not type-checked here (no node_modules).
Also checked and NOT done: the scheduler's `PATTERN_ALERTS_*` switches. The scheduler service has no admin login, no `/control` routes, no state volume and no nginx route, so putting them on the page means new auth in a service that has none; that needs its own change.
