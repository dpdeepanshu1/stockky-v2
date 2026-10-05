# Group 177 — watchlist/sources Tier 1: say why, fetch together, keep Hot Picks when only IPO fails (real-trade-service)

Item 4 of the open list: `watchlist/sources: Tier 1 (api-gateway) empty/unavailable — trying Tier 2`.

## What the line hid
It fitted three different situations: (a) the api-gateway call failed or the breaker is open, (b) the gateway answered
but listed no catalysts (normal outside market hours), (c) only `/surprise/ipo/list` broke. In case (c) a good
`/stockky-hot` answer was thrown away, because both calls were fetched one after the other (up to 25 s each) and
either failing failed the whole tier.

## Fix (`watchlist_engine/sources.py` only)
- `/stockky-hot` and `/surprise/ipo/list` are fetched at the same time.
- If only the IPO list fails, Hot Picks are used without IPO rows and one WARNING names the error. Set
  `WATCHLIST_TIER1_IPO_OPTIONAL=0` to restore "either failing fails Tier 1".
- Errors carry route and type (e.g. `/stockky-hot ReadTimeout`; before, an empty-message timeout logged `call failed ()`).
- The closing line now says which case it was, in brackets: `call failed or breaker open, and no cached copy`,
  `api-gateway down; the cached copy listed no candidates`, or `api-gateway answered but listed no catalysts`.
  The last one is INFO, not WARNING (an empty answer is not an outage). The fall-through to Tier 2/3 is unchanged.

## Not changed
Whether Hot Picks is really empty or the gateway is failing at your open: the new line tells you after the next market
session. Paste the `Tier 1 (api-gateway) empty/unavailable (...)` lines (and any `circuit_breaker[api-gateway]` line
next to them) if it still falls to Tier 2.

## Tests
`tests/test_group177_watchlist_tier1_reasons.py` (14 cases). Sandbox: that file plus the existing watchlist, afterhours and
group 15x/16x suites, 460 passed.

Rebuild real-trade-service.
