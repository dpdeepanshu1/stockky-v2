# Group 179 — Dhan security list loaded in the background, not in the first order (real-trade-service)

Carried-over item: "the Dhan scrip-master load on the first order".

## Cause
`execution/dhan_client.py::get_security_id()` loaded the instrument list (~100k rows via pandas, with a second 30 s
CSV download as fallback) inside the order path. The first order after a restart, and the first one after the 24 h
TTL expired, waited for the whole download, and a failed load was retried by the next order as well.

## Fix
- New background task `keepwarm_security_cache()` (started from `main.startup()`): loads the list ~10 s after boot,
  then re-checks every 6 h and refreshes when the cache is older than 18 h (TTL is 24 h), so orders find a warm cache.
- Retry after a failed load in 5 min; if no Dhan credentials are stored yet (you have not connected), re-check every 30 min.
- The load runs in a worker thread with its own DB session; it never raises and never blocks startup.
- `get_security_id()` is unchanged: if the cache is empty or expired it still loads synchronously, as before.
- `DHAN_SECURITY_WARM_ENABLED=0` turns the task off.
- One INFO line after a load: `Dhan security list warmed in the background (N symbols) ...`.

## Not changed
- A symbol missing from a warm cache (new listing) still raises `SecurityNotResolvedError`; nothing reloads on a miss.
- Whether the list itself is correct (collision handling from the 2026-09-07 fix) is untouched.

## Tests
`tests/test_group179_security_cache_warmup.py` (15 cases). Sandbox: that file with `test_main_routes_core.py` and
`test_dhan_client_remaining_coverage.py` pass (199 + 15). The full real-trade-service run in ONE process shows 4 failures in
`test_group171_held_quote_calls.py` and 1 error in `test_group172_...`; the group 173 code gives the same result in this
sandbox, and each file passes on its own, so it is a cross-test ordering issue that this group did not cause or touch.

Rebuild real-trade-service.
