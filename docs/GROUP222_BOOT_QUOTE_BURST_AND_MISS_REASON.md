# Group 222 - boot quote burst skipped for early pre-open boots, and /quote says why AngelOne-first missed (api-gateway + market-data-service)

Cumulative on group 221. Item 2 of the 2026-10-07 startup-log review.
Rebuild: `docker compose build api-gateway market-data-service && docker compose up -d`.
Files changed: `api-gateway/main.py` (`_surprise_boot_warm_preopen_lead_sec`, `_seconds_to_market_open_ist`, `_surprise_boot_warm_skip_reason`, the surprise warm hook),
`market-data-service/angelone_client.py` (`_note_quote_miss`, `last_quote_miss_reason`, `get_quote`), `market-data-service/main.py` (`_angelone_rest_quote_first`),
`api-gateway/tests/test_main_ws_loops_startup.py` (fixture pins the clock), new `api-gateway/tests/test_group222_boot_warm_preopen.py` (20) and
`market-data-service/tests/test_group222_quote_miss_reason.py` (14).

## What the log showed
Boot at 03:07 UTC (08:37 IST, pre-open): several hundred `/quote/` calls hit market-data within a minute and about half logged
`AngelOne-first did not price X (rate bucket busy...)`. The group 120 skip (restore the saved surprise result, no sweep) only covered `closed` / `holiday`;
08:37 is `preopen`, so the ~1,000-symbol sweep still ran. And the miss line named three causes at once, so you could not tell which one applied.

## Change
1. **Boot warm** (api-gateway): a pre-open boot more than `SURPRISE_BOOT_WARM_PREOPEN_LEAD_SEC` (default 300) before 09:15 IST now takes the same restore-and-skip path
   as a closed market (log: `Startup: market not open yet - restored ... skipped the boot quote sweep`). The cached fast path only honours a result younger than 220 s,
   so a sweep that finishes earlier than ~5 min before the open cannot serve the first cycle. Boots in the last 5 minutes, during the session and after it warm as before.
   If nothing saved can be restored it still warms, as before. `SURPRISE_BOOT_WARM_PREOPEN_LEAD_SEC=0` restores the old behaviour.
2. **Miss reason** (market-data-service): `AngelOneSession.get_quote` records the specific cause on the calling thread and `/quote` logs it:
   rate-limit cooldown, global 403 cooldown, lane budget shed, bucket had no token within N s, AngelOne answered rate-limited, AngelOne returned no quote for the token.
   Return values are unchanged; with no recorded reason the old combined text is logged.

## Not changed
- `/angelone/movers` still sweeps 2,584 quotes at boot. That is the likely budget hog (inferred from log order, not proven); this group removes the sweep that follows it
  for early pre-open boots and makes the next log say which cause applies. Re-check the log after the next 08:3x boot.
- A boot at 09:10-09:15 still sweeps (by design: the result is then useful for the first cycle).

## Tests
Run `bash run_tests.sh` on the VM. The sandbox had no pytest/fastapi/httpx: both new files passed under a stand-in runner against extracted code (34 cases);
the real `test_main_ws_loops_startup.py` and the full suites were not run.
