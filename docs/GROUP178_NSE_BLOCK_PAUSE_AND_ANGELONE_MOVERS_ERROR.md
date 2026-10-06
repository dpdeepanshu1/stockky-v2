# Group 178 — NSE board block pause, AngelOne movers error cache (api-gateway, market-data-service)

Item 6 of the open list: AngelOne movers 403 on login, NSE cookies weak, `quote-equity` 403, NSE live API 0 rows.
**The 403s themselves are not fixed**: NSE blocks this VM's datacenter IP (Akamai) and AngelOne refuses the login
(typical causes: API key/TOTP/client-code mismatch, or the VM IP not on AngelOne's allow-list). That needs your account
and network side. This group stops the code from making the situation worse and makes the cause visible.

## api-gateway (`main.py::_fetch_from_nse_api`)
Each call after a block cost a request, a fresh cookie bootstrap and a retry, for each of the four movers boards, on
every movers recompute. Now, after a 401/403/429 that survives the one session-refresh retry, ALL NSE api fetches pause
for `NSE_API_BLOCK_SECONDS` (default 600; 0 = never pause). During the pause a stale cached value is still served,
otherwise None (callers already fall through to the other sources). One warning marks the start and gives the number of
calls skipped in the previous pause. A 200 ends the pause. 404/5xx/exceptions never start one. Same idea as the group 96
quote-equity pause in market-data-service.

## api-gateway (`main.py::_get_momentum_movers`)
`/angelone/movers` answering `status=error` or `not_configured` came back as HTTP 200 and was ignored with no log line.
One warning per 10 minutes now shows the status and the error text.

## market-data-service (`main.py::/angelone/movers`)
A failed sweep (typically the login 403) was never cached, so every caller tried a fresh login. The error answer is now
cached for `ANGELONE_MOVERS_ERROR_TTL_S` (default 120 s; 0 = off). The warning and the `error` field now include the
exception type (`PermissionError` instead of an empty string). `not_configured` is still not cached.

## What to do about the 403s (your side)
- AngelOne: check `ANGELONE_API_KEY`, client code, PIN and `ANGELONE_TOTP_SECRET`, and whether the VM's public IP is
  registered in your SmartAPI app. The new `angelone/movers failed: <Type>: <message>` and
  `AngelOne movers unavailable (status=error): ...` lines show the exact error.
- NSE: nothing in code gets past the datacenter block; the boards stay empty and movers come from AngelOne, Yahoo and
  bhavcopy. This group only stops the hammering.

## Tests
- `api-gateway/tests/test_main_core.py::TestNseApiBlockPause` (13 cases), `test_main_universe.py` (+2 movers-warning tests);
  `tests/conftest.py` resets the new per-process state between tests.
- `market-data-service/tests/test_group178_angelone_movers_error_cache.py` (10 cases).
- Sandbox: full api-gateway suite 8247 passed; full market-data-service suite 826 passed.

Rebuild api-gateway and market-data-service. Pause state is per process (a restart starts unpaused).
