# group123 (2026-10-04) - yfinance TzCache "File exists" at boot (market-data-service)

Cumulative on group122. Run `bash run_tests.sh` in market-data-service on the VM.

## What the log showed
Three lines at boot: `yfinance: Failed to create TzCache folder '/tmp/yfinance_tz' ... [Errno 17] File exists`.

## State of the code
`market-data-service/main.py` already creates the directory with `os.makedirs(..., exist_ok=True)` before calling `yf.set_tz_cache_location(...)` (same as api-gateway), so concurrent first Ticker calls no longer race on mkdir. The pasted log is from a build older than that code, so a rebuild (`docker compose build market-data-service && docker compose up -d market-data-service`) is what removes the lines. The fix had no changelog entry or test; this group adds both. No production code changed.

## Tests
`tests/test_tz_cache_dir_precreated.py` (2 source-level tests; run standalone here: pass; not run under pytest).

## Not confirmed
If the lines still appear after a rebuild, `/tmp/yfinance_tz` may exist as a file or be unwritable in the container; `docker compose exec market-data-service ls -ld /tmp/yfinance_tz` would show it.
