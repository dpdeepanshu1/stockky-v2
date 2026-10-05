# Group 176 — news: HF 400 pause, parallel news sources (analysis-intelligence-service)

Item 5 of the open list (HF API 400, sequential event fetching). Google News as the only source returning items is
not addressed here (see below).

## Hugging Face 400
`news/main.py::_score_headline`: a 400/401/403/404/410 from the router is a setup problem (model not served to the
token, token without Inference Providers permission, retired model). Every headline got the same answer and logged
`HF API error: 400` again. Now the first such answer pauses the call for `HF_ERROR_BACKOFF_SEC` (default 1800; 0 = never
pause) and logs ONE warning with the first 200 characters of HF's own message and the model in use. Sentiment stays the
neutral 0.0 fallback meanwhile, as before. 429/503 handling and the network back-off (300 s) are unchanged.
**To actually fix the 400:** read the new warning line, then set `HF_MODEL` to a model your HF account lists under
Inference Providers and check the token permission.

## Sequential event fetching
`event/main.py::_fetch_news_from_multiple_sources`: Yahoo, Google News, Moneycontrol, Economic Times and CNBC TV18 were
fetched one after another (time = sum of five network calls per symbol). They now run at the same time (time = slowest).
Merge order is fixed (same as before) so dedupe ("first occurrence wins") and output are unchanged. A source that raises
counts as empty and logs a warning. `EVENT_NEWS_PARALLEL=0` restores the sequential fetch.

## Not changed
- Only Google News returning items: the other feeds' filters/URLs need the live responses to judge; paste a
  `Moneycontrol/Economic Times/CNBC TV18 for <symbol>: N items` log block if you want that looked at.
- Per-symbol events are still fetched one symbol at a time by callers.

## Tests
`tests/test_news_main.py` (+ `TestScoreHeadlineConfigErrorPause`, 18 cases) and `tests/test_event_main.py`
(+ `TestMultiSourceParallel`, 8 cases). Sandbox: news 166 passed; event 287 passed, 1 failed
(`test_QUIRK_raw_feed_is_shadowed_by_symbol_route`, a route-order test that fails the same way on the group 173 code
with this sandbox's starlette version; unrelated).

Rebuild analysis-intelligence-service.
