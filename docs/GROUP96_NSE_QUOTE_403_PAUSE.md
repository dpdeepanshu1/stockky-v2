# group96 (2026-10-04) - item 23: NSE `quote-equity` 403 on every delivery lookup

Cumulative on group95. In `services/market-data-service` run `python3 -m pytest tests -q`, then `docker compose build market-data-service && docker compose up -d`.

## Cause
NSE answers `https://www.nseindia.com/api/quote-equity` with 403 from this VM's datacenter IP for long stretches. `bhavcopy.delivery_from_quote()` returned `None` silently on any non-200, so every symbol needing a delivery % still made a full NSE round trip (adding to the block) and left no log line, before `get_delivery()` fell through to the bhavcopy archives, which is the path that actually works. The cached cookie session that had earned the 403 was also kept for its full 5 minutes.

## Fix (`services/market-data-service/bhavcopy.py`)
- After a **401, 403 or 429** from quote-equity, the quote path is paused for `NSE_QUOTE_BLOCK_SECONDS` (default 600; `0` turns the pause off). While paused, `delivery_from_quote()` returns `None` without any network call, so `get_delivery()` goes straight to bhavcopy.
- The cached NSE cookie session is dropped and closed when a pause starts, so the first call after the pause bootstraps fresh cookies.
- One warning is logged when a pause starts (status, length, how many calls the previous pause skipped). Later 403s inside a pause are silent.
- A 200 ends a pause early. A 404 (unknown symbol), 5xx or an exception never starts a pause.
- Delivery values and sources are unchanged. Bhavcopy results are what you get during a pause, as before.

## Not changed / not verified
- **Two other quote-equity callers in `main.py` are not on this pause:** `_waterfall_nse_direct_price` (price, keeps its own 429-only cooldown) and `_fetch_nse_fundamentals`. They still call NSE each time and fall through on a 403. They share the cached session, so they benefit from the session reset but not from the skip.
- **Why NSE returns 403 is not fixed.** It is the datacenter IP / Akamai block, not something in the code. This only stops hammering it and makes it visible in one log line.
- **Pause state is per process.** A restart starts unpaused and the first call tries NSE once.
- During a pause, delivery % comes from the last bhavcopy session, not live intraday. That was already the case whenever quote-equity returned 403.
- Not live-tested against NSE from here. Behaviour is covered by tests with a fake client.

## Tests
`tests/test_bhavcopy.py::TestQuoteBlockPause` (14 cases): 403/401/429 pause, 404/500/503/exception do not, expiry, 200 clears, `0` disables, bad env value, session dropped and closed, one warning per pause, `get_delivery` falls back to bhavcopy without touching NSE while paused. The autouse fixture resets the pause between tests.

Run here: `test_bhavcopy.py` 84 passed; full `market-data-service` suite 673 passed.
