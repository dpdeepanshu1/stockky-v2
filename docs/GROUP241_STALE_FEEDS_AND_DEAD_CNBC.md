# Group 241 - frozen RSS feeds count as unavailable; dead CNBC TV18 and NDTV Profit feeds get a fallback

From the 2026-10-08 pre-market boot log:
- after-hours scan funnel: `Moneycontrol: 15 items, 15 stale | NDTVProfit: 20 items, 20 stale` - both feeds answered
  200 but contained nothing inside AFTERHOURS_SCAN_MAX_NEWS_AGE_DAYS, so only LiveMint and Business Standard fed the
  catalyst list. Group 238's fallbacks only fired on HTTP failures, so they never ran.
- `news feed https://www.cnbctv18.com/feed/ -> HTTP 404` (event service) - that feed is retired.

## real-trade-service (watchlist_engine/afterhours_scan.py)
- New `_all_items_stale(items)`: items present and EVERY one has a parseable date older than the max-news-age window.
  Undated/unparseable items count as unknown, so such a feed is never called stale.
- `_fetch_rss_items`: for a feed that has `fallback_urls`, an all-stale 200 is treated like a failure and the next URL
  is tried. If nothing fresher turns up, the primary's stale items are returned (funnel keeps showing "N items, N stale")
  with one WARNING naming the reasons. Feeds without fallbacks (LiveMint, Business Standard) behave as before.
- NDTVProfit gets `fallback_urls` (Google News `site:ndtvprofit.com when:1d`) and `publisher: "NDTV Profit"`, which now
  drives the " - NDTV Profit" title-suffix strip (feed key `publisher`, default = source).

## analysis-intelligence-service (news/feed_fetch.py; used by the news pillar AND, since group 240, the event service)
- `_all_entries_stale(parsed, days)` + env `NEWS_FEED_STALE_DAYS` (default 3, 0 = off): for feeds in `_FALLBACKS`, a
  200 whose entries are all older than that is treated as unavailable and the fallbacks are tried (kept if none work).
- `_FALLBACKS` gains CNBC TV18 (404) and the NDTV Profit S3 Atom feed (frozen), each -> a Google News site search with the
  publisher suffix stripped. A fallback result is cached under the primary URL, so the 404 is not re-hit per request.

## Not changed (from the same log)
- HF_MODEL still rejected by Hugging Face (400, "not supported by any provider you have enabled") - needs YOUR env change.
- SMCG04: AngelOne cannot resolve it. From the log order it is most likely the single symbol returned by the ipoalerts
  IPO fallback (an unlisted/placeholder IPO code), which the IPO tracker handles on purpose - not filtered, because
  dropping a real pre-listing IPO would be worse than a harmless warning. Check the IPO tracker tab.
- Yahoo news returning 0 (already auto-skipped for 600 s), gateway NSE bootstrap 403 (bhavcopy fallback covers it):
  need a market-hours log, nothing safe to change blind.
- Every fallback URL is unverified (no network in the sandbox). Check the next log for "served by fallback" and
  "feed is stale".

## Tests
real-trade tests/test_afterhours_rss_blocked_fallback.py now 24 (9 new); analysis-intelligence tests/test_feed_fetch_fallback.py
now 22 (8 new). The older fixture feeds there used a 2026-09-16 pubDate, now 3+ weeks old, so they got fresh dates.
Run through a stand-in runner (no pytest/httpx/feedparser in the sandbox); run `bash run_tests.sh` on the VM.
