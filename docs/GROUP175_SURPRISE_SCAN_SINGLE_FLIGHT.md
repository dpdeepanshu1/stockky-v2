# Group 175 — surprise/scan: one full-universe scan at a time

Item 9 of the open list: `surprise/scan: exceeded 20s -- served last computed result (age 252s)`.

## Cause
`GET /surprise/scan` runs the engine scan as a shielded task and, after `SURPRISE_SCAN_DEADLINE_S` (20 s), serves the
last result flagged stale while the scan carries on. But nothing stopped the NEXT request (Surprise tab, the
real-trade-service candidate poll with `cached=true`) from starting another full scan over the same universe. While a
scan was slow, more scans piled up, which made it slower and the stale answer older.

## Fix (`api-gateway/main.py` only)
- While a default full-universe scan is in flight, later callers join it instead of starting a new one.
- Not shared: calls with `symbols`, and `force_reload=true`.
- A shared scan that fails gives every joined caller the same mapped 500; the slot is cleared when the scan ends.
- Deadline behaviour is unchanged: past 20 s a caller still gets the last result flagged `stale`, `deadline_exceeded`.
- `SURPRISE_SCAN_SINGLE_FLIGHT=0` restores one scan per request.

## Not changed
Why a single scan takes more than 20 s (quote fetches for the whole liquid universe in batches of 25) is not touched.
After the next open, check whether `exceeded 20s` still appears and with what `age`; if it does, the scan itself needs
work and I need that log line.

## Tests
7 new tests in `api-gateway/tests/test_main_surprise_routes.py` (`TestSurpriseScanSingleFlight`). Ran in the sandbox:
that file, `test_main_market_universe_routes.py` and `test_quiet_noisy_loggers.py` (244 passed).

Rebuild api-gateway.
