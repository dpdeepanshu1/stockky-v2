# group129 (2026-10-04) - one news-sources log line per symbol, not one per source

Cumulative on group128. Run `bash run_tests.sh` in analysis-intelligence-service on the VM.

## What the log showed
For every symbol that missed the news cache, `_fetch_headlines` printed seven INFO lines (`Fetched 0 items from _fetch_yahoo_news`, `..._google_news`, `..._economic_times`, `..._business_standard`, `..._ndtv_profit`, `..._livemint`, `..._reuters_india`). A stockky-hot pass over ~100 symbols produced hundreds of these and buried the lines that matter.

## Change (`analysis-intelligence-service/news/main.py::_fetch_headlines`)
- One INFO line per symbol: `news sources for INFY: yahoo_news=0 google_news=1 ... livemint=failed (total 1)`.
- The per-source `Fetched N items from ...` line is now DEBUG.
- A source that raises is still logged at WARNING (`Source X failed: ...`) and shows as `=failed` in the summary.
- No change to what is fetched, deduplicated, sorted or returned.

## Tests
`tests/test_news_main.py::TestFetchHeadlines::test_one_info_summary_line_per_symbol_not_one_per_source`. Sandbox has no httpx/pytest: the real loop body was run against stub sources and printed the expected summary line with no `Fetched ...` INFO lines; the test file compiles, not run under pytest.
