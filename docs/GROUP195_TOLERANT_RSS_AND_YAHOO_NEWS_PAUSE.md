# Group 195 - tolerant RSS parsing in the after-hours scan, Yahoo news auto-pause (real-trade-service, analysis-intelligence-service)

Cumulative on group 194. Item 1 of the remaining list ("weak NSE session and empty catalyst sources"). Rebuild
real-trade-service and analysis-intelligence-service.

## What the 2026-10-06 boot log shows
1. `afterhours-scan: BusinessStandard returned non-XML content (HTTP 200): not well-formed (invalid token): line 269, column 51 - body
   starts '<?xml version="1.0" ...><rss ...'`. A real RSS document was thrown away because strict ElementTree rejected one character
   (usually an unescaped `&` or a control character in a headline/URL), so a whole catalyst source contributed nothing.
2. `Yahoo Finance for <SYM>: 0 items` for 40+ symbols in a row (ABB, BSE, CGPOWER, ANGELONE ...), each still costing a Yahoo request.
   Google News is the only source that returns items; Moneycontrol/Economic Times give 0 because they are 15/51-headline site feeds
   filtered by keyword, and CNBC TV18's feed has 0 entries every time (already cached as empty since group 144).
3. `main._get_nse_client: bootstrap cookies weak (AKA_A2; status 403)`. This is an INFO line, and the same log shows NSE data DID arrive
   (`movers ... gainers +7`, `NIFTY 500 +84`): the NSE home page answers 403 to this VM but the API calls still work, and group 178 already
   pauses all NSE calls for 10 minutes when a real 401/403/429 survives the retry. Nothing here is broken by the weak cookie, so nothing
   was changed for it. A real fix (a different egress IP or a browser-grade TLS client) needs live network access this sandbox does not have.

## Fix
- `real-trade-service/watchlist_engine/afterhours_scan.py`: `_parse_feed_text()` tries strict XML, then XML after removing forbidden
  control characters and escaping bare `&`, then a plain `<item>`/`<entry>` extraction. The "non-XML content" warning now fires only when
  nothing at all can be read (an HTML interstitial). A repaired/regex read is logged once at INFO with the item count.
- `analysis-intelligence-service/event/main.py`: after `EVENT_YF_NEWS_EMPTY_PAUSE_AFTER` (default 30, 0 = never pause) empty Yahoo news answers
  in a row the source is skipped for `EVENT_YF_NEWS_PAUSE_SECONDS` (default 1800); the next lookup is a probe (one more empty answer re-pauses,
  any item resumes). Google News and the RSS feeds are unaffected.

## Not changed
CNBC TV18 (dead feed, cached), the keyword matching of the site-wide feeds, the NSE bootstrap.

## Tests
`real-trade-service/tests/test_group195_tolerant_rss.py` (12), `analysis-intelligence-service/tests/test_group195_yf_news_pause.py` (7).
Sandbox: real-trade 3371 passed, 1 skipped + the 5 that also fail on the uploaded zip; analysis-intelligence 2330 passed + 1 that also fails on
the uploaded zip (`test_QUIRK_raw_feed_is_shadowed_by_symbol_route`). Not live-tested against the real BusinessStandard feed or Yahoo.
