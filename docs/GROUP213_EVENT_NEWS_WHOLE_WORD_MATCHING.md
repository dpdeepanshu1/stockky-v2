# Group 213 - site-feed news is matched on whole words and generic company-name words are not keywords (analysis-intelligence-service)

Cumulative on group 212. The "Not changed" line of group 201 ("keyword matching of the site-wide feeds ... can over-match"). Rebuild
analysis-intelligence-service only.

## What was wrong
`event/main.py` `_fetch_moneycontrol_news`, `_fetch_economic_times` and `_fetch_cnbc_tv18` kept a headline when ANY keyword was a plain
substring of title + description. The keyword list (`_get_keywords`) is the company name, the ticker, aliases AND every word of the company
name longer than 2 characters. So:
- short tickers matched inside other words (BEL in "label", ITC in "pitch", LT in "built"; `news/news_quality.py` fixed this on 2026-10-04,
  item 9, but this module had its own copy);
- words found in hundreds of company names ("limited", "ltd", "india", "bank", "industries", "power", "finance"...) matched most headlines on the
  15-51-item site feeds, so a stock was handed other companies' news and event scores.

## Changes (`event/main.py`)
- `_text_matches_keywords(text, keywords)`: whole-word / whole-phrase match (not preceded or followed by a letter or digit), one cached regex per
  keyword set (cache cleared past 2,000 sets). Keywords under 2 characters are ignored. Used by the three site-feed matchers.
- `_GENERIC_NAME_WORDS` + `_get_keywords`: a company-name word on that list is no longer added as a keyword by itself (surrounding punctuation is
  stripped first, so "Ltd." is caught). Unchanged: the full company name, the ticker (both cases), aliases, and every other name word
  ("Tata", "Larsen", "Toubro", "Kanohar" ...).
- Not touched: Google News queries, the 15-item cap, merge/dedupe, the classifier, `news/news_quality.py`.

## Trade-off
Fewer headlines can match for a company whose only distinctive name word is on the list (e.g. "Bank of India": only the full name and the ticker
remain). Add an entry to `_EVENT_ALIASES` for such a name if you see it under-covered. A name word that is distinctive but shared by a group
("Tata", "Adani") still matches, by design.

## Tests
- New `tests/test_group213_event_keyword_matching.py` (30 cases): whole-word matrix, edge inputs, cache reuse/bound, generic-word drop for five
  company names, aliases kept, bare-ticker fallback, and the three feed functions against a fixed feed (one genuine headline kept, "label" /
  "pitch" / generic-word headlines dropped; a company called "Acme India Bank Limited" matches nothing unrelated).
- `tests/test_event_main.py`: the two keyword tests that pinned "ltd" / "industries" as keywords now pin their absence.
- Sandbox: analysis-intelligence 2391 passed, 1 failed: `test_event_main.py::TestRouteOrdering::test_QUIRK_raw_feed_is_shadowed_by_symbol_route`
  also fails on the uploaded zip before this change (`/events/raw-feed` resolves to `raw_feed`, not `get_events`); not touched here.
