# Group 197 - force_refresh=true now really rebuilds the scan universe (api-gateway)

Cumulative on group 196 (closes the first "Not changed" bullet of that note). Rebuild api-gateway.

## What was wrong
The three scan entry points that take `force_refresh` (`run_scan`, `start_scan`, and the SSE scan stream) drop the live universe key and
call `_build_scan_universe()`. That function serves the durable stale copy whenever the live key is cold, so a forced scan ran on the
stored universe instead of a fresh one (the stream route even built twice: once normally, once after clearing the key, both served stale).
"Force refresh" refreshed the scan results but not the names being scanned.

## Fix
`_build_scan_universe_forced()` runs the real rebuild from group 196 (`_build_scan_universe_fresh`). If another real rebuild is already
running (single flight returns `None`) or the rebuild returns nothing, it falls back to the plain call, so a forced scan never ends up
without a universe. The three `force_refresh` sites use it; non-forced calls are unchanged. A forced scan now waits for the real rebuild
(about 20-30 s cold), which is what asking for a force refresh implies.

## Not changed
- The thin-result guard from group 196 still applies: a rebuild under 50 symbols is used for that scan but is not stored.
- The ~20 other `_build_scan_universe` callers (movers, premarket job, feed, etc.) keep the stale-serve plus background-rebuild behaviour.
- The group 195/196 items of this list not touched here: public frontend hits from 107.150.109.73 (no log line for it in the repo or this
  session; group 149 already closes the public ports and scanner probes) and the WAIT-line noise item (log lines also not available).

## Tests
`tests/test_group197_force_refresh_real_rebuild.py` (4 cases): forced build replaces the stale copy, falls back while a rebuild is running or
when it returns nothing, `start_scan` picks the forced build only when `force_refresh` is true. Existing scan-route tests unchanged and passing.
Not live-tested.
