# group108 (2026-10-04) - item 9: stop paying a Yahoo news call per symbol while Yahoo returns nothing

Cumulative on group107. Rebuild: `docker compose build analysis-intelligence-service && docker compose up -d`.

## Cause
From the VM, Yahoo news (yfinance `Ticker.news`) mostly returns nothing. `_fetch_headlines` still called it first for every symbol, so each lookup paid a yfinance call (and its network wait) before the other six sources ran. The failure was already swallowed, so results were fine, just slower.

## Change (`analysis-intelligence-service/news/main.py`)
- After `YAHOO_NEWS_BACKOFF_AFTER` (default 10) Yahoo lookups in a row that returned nothing or raised, Yahoo is skipped for `YAHOO_NEWS_BACKOFF_SECONDS` (default 600). The first lookup that returns items resets the count. One warning line is logged when the window opens.
- Only Yahoo is skipped. Google News, Economic Times, Business Standard, NDTV Profit, Livemint, Reuters India and NewsAPI (if keyed) run exactly as before, so headlines, scores and summaries are unchanged apart from the wasted Yahoo attempts.
- `YAHOO_NEWS_BACKOFF_AFTER=0` turns it off. Blank env values mean the defaults (same rule as group103). State is per process and thread-safe.

## Trade-off to know
A run of 10 symbols that genuinely have no Yahoo news (small caps) also opens the window, so for up to 10 minutes Yahoo is skipped for symbols that might have had Yahoo news. The other sources still cover them. Raise `YAHOO_NEWS_BACKOFF_AFTER` if you see that matter.

## Not changed
No new news source was added and Yahoo was not removed (item 9's real fix, a replacement source, is still your call). Not verified against live Yahoo from here.

## Tests (`tests/test_news_main.py`, 6 new + an autouse reset fixture)
Opens after N misses and resumes after the window; exceptions count; a hit resets the count; 0 disables; other sources still run while Yahoo is skipped; blank env means defaults.

## Run here
analysis-intelligence-service: 2167 passed (was 2161).
