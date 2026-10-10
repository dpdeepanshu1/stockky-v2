# Group 300 - peer fundamentals: 30 s timeout and one retry on timeout (analysis-intelligence-service)

Follow-up to group 299. The 2026-10-10 boot log: `peer_multi_quarter: Fundamentals fetch failed for CIPLA/DRREDDY/SUNPHARMA/
DIVISLAB/APOLLOHOSP: timed out`, while market-data answered the same requests `200 OK` a moment later (the work finished, the caller
had already given up, and the peer row came back empty).

## What changed (`fundamental/peer_multi_quarter.py`)
- `fetch_fundamentals()` / `fetch_fundamentals_batch()` default `timeout` is now `None` = `PEER_FUNDAMENTALS_TIMEOUT_S`
  (default 30, was a fixed 15; blank, invalid, nan, <=0 or >120 = 30). An explicit `timeout=` argument still wins.
- A timeout (`httpx.TimeoutException`, and only that) is retried `PEER_FUNDAMENTALS_TIMEOUT_RETRIES` times (default 1, range 0-3,
  invalid = 1). By then market-data has the symbol cached, or the retry joins the computation still running there (group 299), so
  it does not start a second Yahoo pass. Connection errors and non-200 answers are not retried and are not cached, as before.
- Worst case for one cold peer is now 2 x 30 s (was 15 s). Peers in a batch run in parallel (6 workers), so a group of five cold
  peers costs one wave. Warm peers (market-data's 24 h cache, this service's 60 s cache) are unchanged and instant.
- `PEER_FUNDAMENTALS_TIMEOUT_RETRIES=0` and `PEER_FUNDAMENTALS_TIMEOUT_S=15` together restore the old behaviour.

## Also fixed (test only)
`tests/test_event_main.py::TestRouteOrdering::test_QUIRK_raw_feed_is_shadowed_by_symbol_route` had been failing since group 152
(it pinned the old bug where `/events/{symbol}` shadowed `/events/raw-feed`; group 213's doc listed it as a known failure).
It now asserts the fixed order: `/events/raw-feed` resolves to `raw_feed`, `/events/TCS` still to `get_events`.

## Tests
`tests/test_group300_peer_fundamentals_timeout.py` (26): default 30 s, explicit timeout wins, batch uses the configured value,
retry once then cached, two timeouts give `{}` after two calls, retries switchable off, connection error and non-200 not retried,
env parsing for both settings. 9 mutations, all caught. analysis-intelligence-service full suite: 2479 passed (single process).
Rebuild analysis-intelligence-service.
