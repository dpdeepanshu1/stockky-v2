# Group 162 — IndianAPI 429 cooldown and per-symbol failure skip

Item 3 of the second open-market log list (analysis-intelligence-service, `fundamental/indianapi_fallback.py`).

## What was wrong
At the open, VINCOFE, SATIN, KOHINOOR, COMSYN, KKCL and DCI each went to IndianAPI again and again and got
HTTP 429 every time. `_fetch_from_indianapi` kept no memory of a rate-limit answer, so every symbol (and every
retry of the same symbol) took a rate-limit slot, made a request, and logged an ERROR. Those requests also
shared the one real IndianAPI budget with the other callers (refill-additional job, weekend hydrator).

## The fix
- **429 starts a process-wide cooldown.** First wait 120 s, doubling for each 429 in a row, capped at 900 s.
  A `Retry-After` header is honoured when it is longer (also capped at 900 s; a non-numeric value is ignored).
  A 429 is caught both as a status code and when it comes through `raise_for_status()`.
- **During a cooldown no request is made for any symbol**, and no rate-limit slot is taken.
- **Other failures** (timeout, connection error, 404, 5xx) keep only that symbol out for 600 s (spelling case ignored).
  The table is capped at 2000 symbols (expired entries are dropped first).
- **A success resets the 429 streak.**
- **Cached data is unchanged:** `get_fundamentals_with_fallback` still serves fresh cache, and still serves stale
  cache when IndianAPI is unavailable; with no cache it returns `None` without a request.
- **Logging:** one WARNING per 429 ("pausing all IndianAPI requests for Ns"), not one ERROR per symbol. Other
  failures still log the same ERROR line as before.
- All helpers swallow their own errors, so a config problem can never break the fundamentals path.

## Settings (blank or invalid values fall back to the defaults)
| Variable | Default | Meaning |
|---|---|---|
| `INDIANAPI_COOLDOWN` | on | `0` / `false` / `no` / `off` restores the old behaviour |
| `INDIANAPI_COOLDOWN_S` | 120 | first 429 wait (doubles each 429 in a row) |
| `INDIANAPI_COOLDOWN_MAX_S` | 900 | cap for the wait and for `Retry-After` |
| `INDIANAPI_SYMBOL_FAIL_TTL_S` | 600 | per-symbol skip after a failure; `0` = off |

## Limits
State is per process (a restart or a second replica starts with a clean slate and finds out with one request).
The 120 s / 900 s / 600 s numbers are my picks, not measured against IndianAPI's real quota window.

## Tests
New `tests/test_group162_indianapi_backoff.py` (30 tests); `tests/test_indianapi_fallback.py` fixture resets the
backoff state. Run:

    python3 -m pytest services/analysis-intelligence-service/tests/test_group162_indianapi_backoff.py services/analysis-intelligence-service/tests/test_indianapi_fallback.py -q

Rebuild analysis-intelligence-service.
