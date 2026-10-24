# E116: new default schedule slots reach an existing runner schedule (closes the E115 "add the 09:20 slot by hand" gap)

Problem: the calibration runner only used `SCHEDULE_DEFAULT` when `schedule.json` did not exist, so on the VM the E115 slot `s_tm_open` (09:20 Time Machine scan + gate check) would never appear.
Fix (`calibration-runner-service/scheduler.py`): the file now records `seen_defaults`. A default slot the file has never been offered is appended once, as shipped. A slot already offered and since deleted is never put back. A file from an older runner is treated as having been offered the five pre-E115 slots (`LEGACY_DEFAULT_IDS`), so only `s_tm_open` is added and nothing you deleted returns. A read-only data dir or damaged file never stops the runner.
Tests: `tests/test_e116_schedule_new_defaults.py` (4). Runner suite 244 passed. No other service touched; the E115 note's "add the slot by hand" line no longer applies.
Rebuild: `docker compose build calibration-runner-service && docker compose up -d calibration-runner-service`.
