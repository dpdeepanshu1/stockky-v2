# Group 240 - event service site feeds use the shared downloader (Moneycontrol 403, no timeout)

`analysis-intelligence-service/event/main.py::_parse_site_feed` (Moneycontrol, Economic Times, CNBC TV18) called
`feedparser.parse(url)`: feedparser's own User-Agent, no timeout, and a bot-gated site (Moneycontrol 403) simply
produced zero entries with nothing logged. Groups 238/239 fixed the same gate in real-trade and the news pillar.

## event/main.py
- New `_load_shared_fetch()`: loads `news/feed_fetch.py` by file path (sub-apps hide each other's folders from
  sys.path) and returns its `fetch_feed_ex`. Loaded once; a load failure is remembered and logged once, and the
  callers fall back to the old `feedparser.parse(url)`.
- New `_site_feed_parse(url)`: shared httpx downloader when available (browser headers, second header profile on
  403/429/..., Moneycontrol fallback URLs, 10 s timeout, status logging), else feedparser. `_parse_site_feed`
  uses it in both places; its own TTL cache (EVENT_FEED_CACHE_SECONDS / EVENT_FEED_EMPTY_CACHE_SECONDS) is unchanged
  and still sits on top. A failed download now yields empty entries instead of an exception.
- Env `EVENT_FEED_SHARED_FETCH=0` restores the direct feedparser call.
- Google News search in the event service (`_google_news_parse`) is NOT changed: Google does not bot-gate RSS, and
  its tests patch `feedparser.parse` directly.

## Tests
- tests/conftest.py: autouse fixture sets `EVENT_FEED_SHARED_FETCH=0` so the existing event tests (which patch
  `em.feedparser.parse`) run exactly as before.
- tests/test_group240_event_shared_feed_fetch.py (7): shared downloader used / env switch off / unloadable module
  falls back and is remembered / real loader finds news/feed_fetch.py / event cache still on top / blocked site gives
  [] / all three portals go through it.
- The existing event tests were NOT re-run in the sandbox (they need fastapi/yfinance/pytest); the 7 new tests were
  run through a stand-in runner with stubs. Run `bash run_tests.sh` on the VM.
