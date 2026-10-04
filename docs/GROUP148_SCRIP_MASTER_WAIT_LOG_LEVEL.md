# group148 (2026-10-04) - boot-time "scrip master not loaded yet" no longer logs at ERROR

Cumulative on group147. Rebuild market-data-service: `docker compose build market-data-service && docker compose up -d`.

## Cause (from the 8 h VM log audit)
`angelone_ws_feed.py` logged `AngelOne feed: scrip master not loaded yet (attempt 1) - retrying in 10s` at ERROR on every boot. The scrip master downloads in the background after start, so the first tries normally miss it and the feed recovers by itself. An ERROR on every healthy boot hides real errors and trips error greps.

## Fix
- `_scrip_wait_log_level(attempt)`: WARNING for attempts 1-3 (about the first minute: 10 s, 20 s, 30 s waits), ERROR from attempt 4. Retry/backoff behaviour is unchanged.
- The "resolved 0/N symbols" ERROR (scrip master loaded but nothing resolves) is untouched.

## Tests
`tests/test_angelone_scrip_wait_log_level.py` (3 tests). market-data suite: 760 passed.

## Log audit: other lines, no code change
- `AngelOne quote(batch) returned HTTP 403 ... exceeding access rate` (once): already handled by the 30 s cooldown and the once-per-60 s log throttle.
- `NSE bootstrap cookies weak (403)`: same VM-IP block as Yahoo (item 6); bhavcopy fallback active.
- `BOOT FORENSICS cause=FRESH_CONTAINER`, `AUTH CONFIG`, `SURPRISE_UNIVERSE not set`: informational.
- Yahoo still returns 429 to `curl -4` (2026-10-04).
- The window contained only a boot (no market session), so no trading-path errors could appear. Re-run the audit after a market session for a real check.
