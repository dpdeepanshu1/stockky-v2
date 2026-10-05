# Group 173 - boot burst: start the live feeds once, on the real universe

Cumulative on group 172. Item 7 of the open list ("the feed grew from 250 to 491 symbols a minute after start").
Rebuild market-data-service. Only `market-data-service/main.py` changed (plus tests).

## What was wrong
At boot both live feeds (AngelOne, Yahoo) started at once on the built-in default universe (~250 symbols). About
20 s later the first `/scan/universe` answer (~491 symbols) arrived and the refresh loop restarted the AngelOne feed:
stop (a join of up to 10 s), a second AngelOne login, a second token-resolve and subscribe wave - all inside the first
minute after a restart, against the rate limits the open already strains.

## The fix
- When `API_GATEWAY_URL` is set, the two startup hooks no longer start the feeds. The first successful
  `/scan/universe` answer starts them, once, on the real universe (the refresh loop now also starts the Yahoo feed,
  idempotently, before subscribing; the AngelOne stop is a no-op when nothing is running).
- Safety net: if no universe has arrived `FEED_BOOT_UNIVERSE_WAIT_S` (default 60 s) after boot, one fallback task
  starts both feeds on the default universe, exactly as before. The refresh loop still re-points them later.
- `FEED_BOOT_WAIT_FOR_UNIVERSE=0` (or no `API_GATEWAY_URL`, or a wait of 0) restores the old immediate start.
- Logs: `feed boot: waiting up to 60s for the first scan universe ...`; a WARNING if the fallback fires.

## Things to check
- With the gateway slow to boot, the live feeds now start up to ~60 s later than before (default universe) instead of
  immediately; quotes in that window come from the REST path, as they did for any non-default symbol anyway.
- After a restart the log should show one `feed universe refreshed: N symbols` and one AngelOne login, not two.

## Not changed
The second half of item 8 ("the pause state resets on restart") is not addressed: it is not clear which pause it
means. The group 160 dead-symbol pause and the group 172 no-history pause are per process by design. Tell me which
one you mean and whether it should be saved (database or Redis) across restarts.

## Tests
New `market-data-service/tests/test_group173_boot_feed_defer.py` (25 cases). Not run with real pytest/fastapi
(sandbox has neither): the new code was extracted from `main.py` into a stub module and all 25 cases passed under a
stand-in runner. The full market-data suite was not run.
