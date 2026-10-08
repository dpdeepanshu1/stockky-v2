# Group 250 - "momentum-movers source failed ()" now says why, and waits as long as the gateway does (2026-10-08 log)

## What the log showed
real-trade-service's dynamic universe logged `dynamic_universe: momentum-movers source failed ()`. An httpx timeout has an empty
`str()`, so the cause was invisible (group 170 fixed the same thing for the `/check` line only). The call also used a 15 s client
timeout, while api-gateway's `/market/momentum-movers` can legitimately take longer on a cold start: it runs the NSE boards, the
AngelOne whole-market sweep and the yfinance fallback, and lets a second caller wait up to `MOMENTUM_MOVERS_JOIN_WAIT_S` (45 s) for
the first (group 189). The boot log shows that computation finishing about a minute after startup, so a 15 s client gave up first.

## real-trade-service
- `watchlist_engine/dynamic_universe.py`
  - `_err_text(e)`: `ReadTimeout` for an exception with no message, `Type: message` otherwise. Used by the momentum-movers,
    volume-shock, subscribe, unsubscribe and `/check` warnings.
  - The momentum-movers log is now `momentum-movers source failed after 15s (ReadTimeout), continuing with volume-shock only`.
  - Movers client timeout is `DYNAMIC_UNIVERSE_MOVERS_TIMEOUT_S` (default 45, blank / non-numeric / <= 0 / nan fall back to 45).
    The volume-shock client stays at 15 s.

## Limits
- A failure is still non-fatal (volume-shock alone is used). Only the wait is longer, and only on the 20-minute refresh cycle, so
  nothing on the trading path waits longer.
- Not confirmed live. After the next restart the log should either show no movers failure or name its type and elapsed time.

## Tests
New `tests/test_group250_movers_error_text_timeout.py` (16 cases): `_err_text` variants; timeout default / bad values / override;
movers client receives the configured timeout while volume-shock keeps 15 s; log names `ReadTimeout` and elapsed time with no
`failed ()`; volume-shock failure names its type; message kept when present.
Real pytest, real-trade-service: 3689 passed; the one `test_group172` teardown error is also on the unmodified upload.
