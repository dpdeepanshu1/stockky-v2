# Group 187 - market-cap fetch: say why it failed, keep the last good value

Cumulative on group 186. Items 4 and 5 of the 2026-10-06 boot-log list. Rebuild real-trade-service.

## Item 5 - `market_cap fetch failed for HFCL ()`
The standard track's market-cap fetch (`candidate_engine/candidates.py::_fetch_market_cap_cr`, fundamental `/analyze`,
12 s) timed out for HFCL and REDINGTON at boot. httpx timeouts stringify to '', hence the empty parentheses. Worse, a
failed fetch means no market-cap floor for that candidate (missing data is deliberately not a reject), so a small cap
could slip through whenever the fundamental service was slow.
- The INFO line now names the cause: `ReadTimeout`, `HTTP 500`, `no market_cap in the answer`, or `Type: message`.
- Market caps move slowly: the last good value is kept per symbol (in memory) and used when a later fetch fails, up to
  `CANDIDATE_MCAP_STALE_TTL_S` (default 86400 s, 0 = off). The line then ends `- using last known Rs N cr`. A symbol
  never fetched successfully in this process still returns None (no floor), as before.
- `CANDIDATE_MCAP_TIMEOUT_S` (default 12) sets the request timeout.
- `/quote` was considered as a second source and rejected: its `market_cap` is never filled (owned by the fundamental
  service), so it would not help.

## Item 4 - fundamentals timeout for HFCL / REDINGTON: no code change
analysis-intelligence-service already calls market-data `/fundamentals` with a 60 s timeout through `md_guard`
(group 170: slot limit, cool-down after repeated timeouts, timeout class named in the log). At that boot market-data was
busy (AngelOne 403s, yfinance 18 s hard timeouts, the 2707-symbol sweep) and the fundamentals only finished after the
caller gave up (`Fundamentals for REDINGTON.NS: PE=18.29` was logged afterwards). The cause is load on market-data
(items 7, 8, 11 of the list), not a timeout set too short. The IndianAPI fallback then got HTTP 429 and paused 120 s, as
designed.

## Tests
New `real-trade-service/tests/test_group187_market_cap_failure_reason.py` (11 cases); `tests/conftest.py` clears the
last-good map between tests (the existing `TestFetchMarketCapCr` cases call TCS repeatedly). No pytest/httpx/sqlalchemy
in the sandbox: the 11 new cases pass under a stand-in runner with stubbed imports; the full suite was not run.
