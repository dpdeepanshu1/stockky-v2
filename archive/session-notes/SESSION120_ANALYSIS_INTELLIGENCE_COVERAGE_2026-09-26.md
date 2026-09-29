# Session 120 — analysis-intelligence-service: initial coverage pass (2026-09-26)

## Files added (142 tests, 142/142 passed)

### `tests/test_rate_limit_report.py` — 19 tests
`rate_limit_report.py` (190 lines): both public functions fully covered.
- `record_rate_limit_hit`: writes event+stats, truncates at 500, merges prior
  stats, handles dict-format prior events, gateway POST when URL set, no POST
  when URL absent, swallows kv failure, provider/detail truncation.
- `report_if_rate_limited`: 429/503 status, rate-limit message, quota message,
  false for generic error, response attr extraction, None input, calls record.

### `tests/test_news_quality.py` — 40 tests
`news/news_quality.py` (226 lines): all 5 public functions + 2 helpers.
- `expand_keywords`: symbol, .NS/.BO strip, company name words, Ltd suffix,
  short words excluded, PWL/LGEINDIA extra aliases, dedup, empty/None.
- `_is_relevant`: title hit, desc hit, no match, single-char skip, case-insensitive.
- `_parse_entries`: relevant kept, irrelevant excluded, old cutoff, recent
  included, max_items, bad date no crash, HTML stripped, empty feed.
- `fetch_multi_source`: returns list, dedup by title, max 25, exception swallowed, sorted newest first.
- `summarize_headlines`: empty → no-news msg, extractive fallback, LLM used, LLM failure → extractive, LLM empty → extractive, max_bullets.
- `build_news_response`: full structure, empty→50, positive score, negative score, score 0-100 clamped, keywords included, headlines ≤12.

### `tests/test_shared_adaptive.py` — 22 tests
`technical/shared_adaptive.py` (105 lines): all 4 functions.
- `percentile_rank`: empty→50, above-all→100, below-all→0, median, native float.
- `adaptive_gate`: thin sample (above/below guardrail), full sample, threshold clamped to max/min, native bool.
- `hybrid_gate`: thin→False, passes pctl+floor, below abs_floor→False, low percentile→False, native bool.
- `relative_strength_vs_sector`: no data, thin peers, good return vs many peers, None peer return excluded, self excluded from window.

### `tests/test_peers.py` — 28 tests
`fundamental/peers.py` (145 lines): all 3 public functions.
- `normalize_sector`: symbol lookup wins, all 13 sector mappings, unknown→None, None raw+no symbol.
- `peers_for`: IT symbol, bank symbol, sector override via raw string, unknown, .NS strip.
- `average_metrics`: averages PE, skips None, pe alias, empty rows, non-dict row, multiple metrics.
- `peer_relative_score`: no peer avg→50, empty avg→50, cheaper PE raises, expensive PE lowers, higher ROE raises, higher growth raises, lower debt raises, clamped 0-100, pe=0 skipped.

### `tests/test_indianapi_fallback.py` — 33 tests
`fundamental/indianapi_fallback.py` (222 lines): all functions.
- `_add_trading_days`: skips weekends, 0 days, 5 days → next Monday.
- `_cache_expiry`: after N trading days, time is NSE market open (09:15).
- `_is_cache_fresh`: fresh → True, stale → False, missing → False, invalid → False.
- `_cache_get/_cache_set`: stored value, exception → None, kv=None, set stores, swallows exception, noop when None.
- `_enforce_rate_limit`: mem fallback, rate_limiter.acquire used when available.
- `_fetch_from_indianapi`: None when no key, JSON on success, None on exception.
- `get_fundamentals_with_fallback`: yahoo returned, indianapi on None, fresh cache used, stale refreshed, stale returned when fetch fails, both fail→None, yahoo exception→fallback.

## Remaining analysis-intelligence-service files (deferred)
By line count: `event/main.py` (1240), `technical/main.py` (766),
`fundamental/main.py` (660), `news/main.py` (658), `sentiment/main.py` (410),
`event/event_depth.py` (317), `fundamental/peer_multi_quarter.py` (386),
`fundamental/wire_peer_multi_quarter.py` (not listed but exists),
`fundamental/kv_cache.py`, `fundamental/oracle_compat.py`,
`fundamental/rate_limiter.py`, `main.py` (134).
