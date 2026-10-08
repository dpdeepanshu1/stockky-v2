# Group 236 - closed market: leave out the quota-limited quote sources

Cause (2026-10-07 evening log): QUALIANCE and BMISL are in no bhavcopy and Yahoo has nothing for them. Each
`/quote/{sym}` walked NSE-direct -> AngelOne -> Yahoo -> IndianAPI -> TwelveData -> AlphaVantage -> Polygon at
00:55 IST and tripped `indianapi cooldown until +120s (rate limit / 429)` and `alphavantage cooldown until +300s`.
With the market closed those sources can only return the same last close, so the quota was spent for nothing.

## market-data-service (main.py)
- New `_quote_closed_skip_quota_sources()`: True when the market is closed (same `_quote_market_closed()` as
  group 233) unless `QUOTE_CLOSED_SKIP_QUOTA_SOURCES=0`. Fails open (False) if the clock check raises.
- `/quote/{symbol}` waterfall: while that is True, IndianAPI and the TwelveData / AlphaVantage / Polygon block are
  not called. NSE-direct, AngelOne REST and Yahoo still run, so a symbol one of them knows is still priced.
- Open market: unchanged. Index symbols: unchanged.
- `/quotes/bulk` already did not reach these sources while closed (group 233).

## Trade-off
A brand-new listing that only IndianAPI knows gets no price after hours until the market opens or bhavcopy has it.

## Tests
market-data tests/test_group235_closed_skip_quota_sources.py (7).
