# group122 (2026-10-04) - "no last-known movers cached yet" printed 4x per pass

Cumulative on group121. Run the api-gateway suite on the VM.

## What the log showed
After every boot (and every Movers poll while the market is closed and nothing has been saved yet) api-gateway printed
`Market session phase=closed and no last-known movers cached yet - returning empty rather than a guaranteed-empty fetch` four times in a row.

## Cause
`_get_nifty50_data()` is called by the three Movers routes (top-gainers, top-losers, most-active) and by the momentum-movers collector. With no last-known list saved yet, each caller took the same "return empty" branch and logged the same line.

## What changed (`api-gateway/main.py`)
The line is logged at INFO once per phase per 600 s (`_MOVERS_EMPTY_LOG_WINDOW_SEC`); repeats inside the window go to DEBUG. A different phase has its own window. The return value (an empty list) and the "serve last-known" path are unchanged. No env var.

## Tests
`tests/test_main_scan_runner.py`: `mv` fixture clears the throttle state per test; new `test_nothing_known_line_is_logged_once_per_window_then_debug`. Sandbox has no pytest/fastapi: the real branch was run against stubs (4 calls -> 1 INFO + 3 DEBUG) and the files compile; the new test is unrun here.

## Not changed / needs a check on the VM
- The gateway warning "No supported WebSocket library detected" after an outside scanner hit port 8000: `api-gateway/requirements.txt` already has `uvicorn[standard]==0.30.1`, which installs `websockets`. So the running image probably predates that line or the install failed. Check with `docker compose exec api-gateway pip show uvicorn websockets`; if `websockets` is missing, `docker compose build --no-cache api-gateway && docker compose up -d api-gateway`. No code change made.
- The Movers list stays empty until one open-session fetch saves a last-known list (unchanged, as in group84).
