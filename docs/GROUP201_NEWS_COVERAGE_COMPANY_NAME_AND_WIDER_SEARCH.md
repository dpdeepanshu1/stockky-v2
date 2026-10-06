# Group 201 - thinner-than-necessary news coverage: company-name fallback expires, thin Google News searches are widened (analysis-intelligence-service)

Cumulative on group 200. Item 3 of the open list ("thin news and event coverage"). Rebuild analysis-intelligence-service.

## What the repo shows
- Group 195 found that Google News is the only news source that returns items for most symbols (Moneycontrol / Economic Times are
  15-51-headline site feeds filtered by keyword, CNBC TV18 is empty, Yahoo returned 0 items for 40+ symbols in a row).
- Google News was asked ONE question: the "company name" from `_get_company_name`. When yfinance is rate-limited or has no name, that
  function returns the bare ticker and **cached it for the whole process life** (`COMPANY_NAME_CACHE_TTL` was defined but never used; the
  old test `test_QUIRK_rate_limit_fallback_is_cached_for_the_process_lifetime` pinned it). One transient cool-down at boot meant
  Google News was searched by ticker ("ABB") for every later lookup, even after yfinance recovered.
- A symbol whose single search returned 0-2 items had no second chance.

**Not verified:** this sandbox has no network, so I could not see how many items a real Google News query returns for any symbol, and the
2026-10-06 logs do not show the company name used per query. The fix targets the two causes visible in the code; the effect on item counts
needs the new log line (below) from your VM.

## Fix (`analysis-intelligence-service/event/main.py`)
- `_get_company_name`: a bare-ticker fallback is remembered for `EVENT_COMPANY_NAME_FALLBACK_TTL_S` (default 600 s; 0 = old behaviour, never
  asked again); after that the real name is fetched again. A real name is cached for good as before. New dict `_company_name_fallback_until`.
- `_fetch_google_news`: if the first search worked but returned fewer than `EVENT_GN_THIN_BELOW` items (default 3; 0 = never widen), a second
  search is made and merged (first occurrence of a title wins, same 30-day cut-off): `"<name without Limited/Ltd/Private...> share price NSE"`,
  or `"<TICKER> NSE share price"` when only the ticker is known. A failed or unreadable first search is not asked twice. The first query URL is
  unchanged. INFO line: `Google News widened for <SYM> with '<query>': N item(s) after the second search`.
- The source list, the merge/dedupe/sort in `_fetch_news_from_multiple_sources`, the 15-item cap and the site-feed keyword matching are unchanged.

## Cost
At most one extra Google News request per symbol lookup, only for thin results; lookups are cached for 4 h (1 h when empty), so a 100-symbol scan
adds at most ~100 requests per cache period.

## Not changed
Keyword matching of the site-wide feeds (substring match on company-name words, which can over-match), the 15-item cap, CNBC TV18, Yahoo news.

## Tests
- New `tests/test_group201_news_coverage.py` (31 cases): fallback expiry (window, refetch, new window after a repeat failure, exception path,
  TTL 0, env parsing), extra-query wording, widening (thin / empty / not thin / failed first / bozo first / failed second / duplicates / old items /
  threshold env / first URL unchanged).
- `tests/test_event_main.py`: the QUIRK test is rewritten to pin the new behaviour; `_clean` also clears `_company_name_fallback_until`.
- `tests/conftest.py`: autouse `EVENT_GN_THIN_BELOW=0` so existing tests that mock a single `feedparser.parse` call are unaffected.
- No pytest/fastapi/httpx/numpy in the sandbox: the 31 new cases ran under a stand-in runner with stub modules; `test_event_main.py` was NOT run
  (needs numpy/pandas/starlette). Run on the VM: `cd services/analysis-intelligence-service && python -m pytest tests/test_group201_news_coverage.py tests/test_event_main.py -v`.

## After you rebuild
`docker compose logs --since 30m analysis-intelligence-service | grep -E "Google News widened|Google News for"` - the per-symbol counts show whether
coverage rose. Paste a few lines.
