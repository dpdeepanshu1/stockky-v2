# Group 238 - Moneycontrol RSS 403 in the after-hours scan: header retry, fallback URLs, blocked cool-down

Cause: `https://www.moneycontrol.com/rss/latestnews.xml` answers the deploy VM with HTTP 403 (bot gate), so the
after-hours / intraday news scan got 0 items from its highest-weighted feed (source_bonus 10). `_fetch_rss_items`
had one URL, one header set, and logged a generic "RSS fetch failed" with no recovery. The ET feeds (session58)
were a different problem (retired URLs, HTTP 200 HTML) - this is a real gate on a live feed.

## real-trade-service (watchlist_engine/afterhours_scan.py, config.py)
- `_fetch_rss_items` now tries `feed["url"]`, then each `feed["fallback_urls"]` in order, stopping at the first
  readable feed. A feed with no fallbacks behaves as before.
- A bot-gate status (401/403/406/429/451) retries the SAME url once with `_RSS_ALT_HEADERS` (Safari UA, fuller
  browser headers) before the url counts as failed. Other statuses (5xx), non-XML bodies and transport errors go
  straight to the next url.
- Moneycontrol fallbacks: `rss/business.xml`, then a Google News RSS search `site:moneycontrol.com when:1d`
  (Google does not bot-gate RSS). Items served by a fallback have the trailing " - Moneycontrol" removed from the
  title so symbol extraction/scoring see the native headline.
- If EVERY url fails with a bot-gate status the feed is skipped for `AFTERHOURS_RSS_BLOCKED_COOLDOWN_SECONDS`
  (default 1800, floor 60) - no hammering on every 15-min intraday tick. 5xx / transport failures do not start the
  cool-down (retried next tick as before). Success clears it.
- Logs: INFO "<src> primary feed unavailable (<reasons>) - served by fallback <url>: N item(s)"; WARNING
  "<src> blocked on all N URL(s) (<url>: HTTP 403 | ...) - skipping this feed for N s".

## NOT live-verified
The sandbox has no network. The two fallback URLs are from knowledge of Moneycontrol's/Google's public feeds, not
curl-tested. After deploy, the first scan log shows which one answered. If the "served by fallback" line never
appears and the "blocked on all" WARNING does, send me those lines.

## Not changed
analysis-intelligence-service/news/feed_fetch.py downloads the same Moneycontrol feed with the same UA and will
see the same 403 (it already logs the status per source). Left for the next round.

## Tests
real-trade tests/test_afterhours_rss_blocked_fallback.py (15). The 5 existing TestFetchRssItems tests were also run.
pytest/httpx are not installed in the sandbox: all 20 were run through a stand-in runner with stub httpx; run
`bash run_tests.sh` on the VM for the real result.
