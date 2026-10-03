# Item 9 - News pillar looked dead (2026-10-04)

Symptom: every "Fetched N items from <source>" line in analysis-intelligence-service/news said 0 (all sources,
~25 symbols); the pillar then defaulted to a neutral 50.

Cause found in code (no network in the sandbox, so the live feed responses could not be re-checked here):
- news/main.py and news/news_quality.py called `feedparser.parse(url)` directly: urllib, "feedparser/x.y" User-Agent,
  no timeout. Bot-gated feeds answer that with an empty/HTML body and feedparser never raises -> silent 0 entries.
  real-trade-service's after-hours scan uses a browser UA over httpx and gets items from the same hosts.
- NDTV Profit pointed at www.ndtv.com/business/rss / feeds.feedburner.com/ndtvprofit-latest (not live); the Atom feed
  afterhours_scan verified live is prod-qt-images.s3.amazonaws.com/.../bloombergquint/feed.xml.
- General "latest news" feeds were re-downloaded for every symbol of every request (hot-picks scan = ~175 identical GETs).
- news_quality._is_relevant matched 2-4 letter tickers as plain substrings (LT, BEL, ITC, PNB -> "built", "label", "pitch").
- /analyze passed company_name=None, so only the bare ticker was matched ("TCS", not "Tata Consultancy Services").

Fix: news/feed_fetch.py (httpx, browser UA, 10 s timeout, per-URL cache 300 s / Google search 120 s / failures 60 s,
WARNING that names HTTP status / "HTML instead of a feed" / "200 but 0 entries"); both news modules use it;
NDTV URL fixed; whole-word match for short keywords; NAME_HINTS company name passed to the quality path;
/analyze now returns sources_status + sources_failed (quality path) and data_quality.level="none" when nothing matched.

After deploy, grep the news log for "news feed" WARNING lines: they name the sources that are actually blocked/retired.
Env: NEWS_FEED_TIMEOUT_SEC (10), NEWS_FEED_CACHE_TTL_SEC (300; 0 = off). Tests: tests/test_feed_fetch.py (new) +
additions in test_news_quality.py / test_news_main.py (autouse fixture stubs feed_fetch._download).
