# group147 (2026-10-04) - feed-universe refresh warning no longer ends with a blank cause

Cumulative on group146. Rebuild market-data-service: `docker compose build market-data-service && docker compose up -d`.

## Cause (from the VM log audit)
`market-data-service/main.py::_refresh_feed_universe_loop` logged
`feed universe refresh: fetch failed, keeping existing feed: ` with nothing after the colon. The line formatted `str(e)`, and many httpx errors (ReadTimeout, ConnectTimeout, RemoteProtocolError) have an empty `str()`, so the cause was invisible.

## Fix
- New helper `_exc_detail(e)` returns `TypeName` or `TypeName: message`. The warning now ends e.g. `...keeping existing feed: ReadTimeout`.
- Behaviour unchanged: a failed fetch still keeps the existing feed and retries after `FEED_UNIVERSE_RETRY_DELAY_S` (60 s).

## Tests
`tests/test_feed_universe_fetch_error_detail.py` (3 tests, including one that drives the real loop with a client that raises `ReadTimeout("")`). market-data suite: 757 passed.

## Log audit: the other lines, no code change
- `AUTH CONFIG` lines matched only because of the word "timeout" (`session_idle_timeout_minutes`). Informational.
- `SURPRISE_UNIVERSE/SCAN_UNIVERSE not set`: expected, logged once per process (it appeared twice because market-data-service was restarted).
- `NSE live API ... 0 securities rows -> bhavcopy fallback (2662 symbols)` and `_get_recent_ipos: using static fallback list`: NSE's bootstrap cookies are weak (HTTP 403) on this VM, the same IP-level block as Yahoo (item 6). Fallbacks work; no code fix.

## Item 6 closed
Yahoo returns 429 to every request from the VM's IPv4 address (`curl -4`), `curl -6` cannot connect (no IPv6). Not fixable in code; AngelOne and Google News cover quotes and news. A proxy (`YAHOO_PROXY`) is the only route if Yahoo data is wanted back.
