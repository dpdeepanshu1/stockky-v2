# Group 239 - Moneycontrol HTTP 403 in the news pillar (analysis-intelligence-service)

Same gate as group 238 (after-hours scan): `news/main.py::_fetch_moneycontrol` and `news/news_quality.py` download
`moneycontrol.com/rss/latestnews.xml` through `news/feed_fetch.py` with one browser header set, so a 403 meant 0
Moneycontrol items for every symbol (the 403 was logged per source, but there was no recovery).

## news/feed_fetch.py
- `_download`: a bot-gate status (401/403/406/429/451) retries the same URL once with `FEED_HEADERS_ALT`.
- `_FALLBACKS` registry: Moneycontrol -> `rss/business.xml`, then a Google News RSS search `site:moneycontrol.com
  when:1d`. Tried in order only when the primary download fails; first fallback with entries wins, is cached under
  the primary URL and reported in `info["via"]`. Google titles' trailing " - Moneycontrol" is stripped. Env
  `NEWS_FEED_FALLBACKS=0` turns this off.
- All URLs blocked -> remembered for `NEWS_FEED_BLOCKED_TTL_SEC` (default 1800, 60..86400) instead of 60 s, so a
  gated feed is not re-downloaded on every symbol request. Non-block failures keep the 60 s memory.
- Fallback URLs are NOT live-verified (no network in the sandbox). Check the next news log for
  "news feed ... served by fallback ..." or "... and all fallbacks failed: ...".

## Not changed
`event/main.py::_parse_site_feed` still calls `feedparser.parse(url)` directly (feedparser's own User-Agent, no
timeout) for Moneycontrol/ET/etc. Its tests monkeypatch `em.feedparser.parse(url)`, so moving it onto feed_fetch needs
those tests reworked - left for a separate round.

## Tests
analysis-intelligence-service tests/test_feed_fetch_fallback.py (14). Existing tests/test_feed_fetch.py also run.
pytest/httpx/feedparser are not installed in the sandbox: run through a stand-in runner with stubs; run
`bash run_tests.sh` on the VM for the real result.
