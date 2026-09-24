# Session 91, round 2 (2026-09-24): afterhours_scan.py — fetcher coverage

Continues round 1 (`SESSION91_AFTERHOURS_PURE_HELPERS_COVERAGE_ROUND1_2026-09-24.md`,
`tests/test_afterhours_scan_pure_helpers.py`, 31 tests on the 5 pure
helpers). This round: `tests/test_afterhours_scan_fetchers.py` (20 tests),
covering the three network/circuit-breaker-dependent functions.

Mocking approach mirrors what's already established in this repo's
`tests/test_watchlist_sources.py` (session89) for the identical
`api_gateway_breaker` + `httpx.AsyncClient` pattern: patch
`httpx.AsyncClient` to return a fake `AsyncMock` client, and patch
`watchlist_engine.afterhours_scan.api_gateway_breaker` with a stand-in whose
`.call()` either runs `fn()` directly (closed-breaker case) or always uses
`fallback()` (open-breaker case) — no real network, no real breaker state.

- `_fetch_rss_items` (5 tests): successful RSS parse, non-XML/malformed
  body (`ET.ParseError` path), HTTP error status, a raw connection
  exception, and an empty-but-valid feed body — all four failure shapes
  degrade to `[]`, never raise.
- `_fetch_bulk_deal_hits` (11 tests): open breaker → `{}`, empty payload →
  `{}`, missing-symbol item skipped, no-date item degrades open (kept),
  an all-stale-dates item dropped, a fresh date via
  `insider_transactions[].date` kept, non-numeric `score` falling back to
  `_CATALYST_BASE_SCORE["bulk_block"]`, negative score clamped to 0 and
  dropped, score >100 clamped to 100, and same-symbol dedup both ways
  (higher score replaces, lower score doesn't).
- `_validate_symbols` (4 tests): empty input short-circuits with no call,
  price-based filtering (zero/`None` price excluded), a symbol absent from
  the preview dict excluded, and the exception → degrade-open (returns the
  input unchanged as a set) path.

One mocking detail worth flagging in case it trips up the real run: a bare
`MagicMock` response object is itself callable, so `AsyncMock(side_effect=resp)`
would make the mock library treat `resp` as a side-effect function and
*call* it (returning a fresh auto-mock) instead of returning `resp`. The
helper in this file (`_fake_async_client`) branches on whether the passed
effect is an exception, a list, or a plain object, using `return_value=`
for the plain-object case specifically to avoid that trap.

**Still not covered** (round 3, deferred — need a real in-memory-SQLite
`db` fixture on top of the httpx/breaker mocking above): `run_afterhours_scan`
and `finalize_nextday_watchlist`, the two DB-writing orchestrators. These
are the last pieces of this file.

## Verification status — same caveat as round 1

**Not run through live pytest** — this sandbox still has no network access.
Written and hand-traced against the real source, including re-reading
`resilience/circuit_breaker.py`'s `CircuitBreaker.call()` signature and
`market_feed/feed.py`'s `get_preview_quotes` return shape to get the mocks
right, not executed. Run for real before trusting it:

```bash
cd services/real-trade-service
python -m pytest tests/test_afterhours_scan_fetchers.py tests/test_afterhours_scan_pure_helpers.py tests/test_afterhours_extract_symbol.py -v
```

No production code changed this round — tests only.
