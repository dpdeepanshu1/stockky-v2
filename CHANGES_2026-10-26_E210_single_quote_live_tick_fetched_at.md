# E210 - market-data: GET /quote/{symbol} no longer stamps an old live-feed tick as fresh

**Where:** services/market-data-service/main.py, `_get_quote_inner`, the AngelOne-WS and Yahoo-WS live branches. Open item from E188 ("same pattern, left for a later round").
**Bug:** both branches accepted a feed tick up to 20 s old (`get_live_quote` default) and set `fetched_at` to the time of the READ, so a 19 s old price was returned as 0 s old and cached under that stamp. Callers that judge freshness by `fetched_at` were told it was fresh.
**Fix:** `fetched_at = _live_tick_fetched_at(live)` (the E188 helper): the tick's own `ts`, never in the future, `now` when `ts` is missing or unusable. No new setting: the existing `QUOTE_BULK_LIVE_TICK_TS=0` also restores the old stamping here (its name says BULK, but one switch for both paths keeps the behaviour consistent).
**Behaviour to expect:** `age_s` on a feed-sourced /quote answer is now the tick's true age (up to ~20 s) instead of ~0.
**Tests:** tests/test_e210_single_quote_live_tick_fetched_at.py (12 cases). NOT RUN (pytest not installed here); py_compile only. `docs/ENV_REFERENCE.md --check` passes.
**Not touched (still your call, from E194):** the exit loop acts on a stale last-good tick (up to FEED_PRIORITY_STALE_FALLBACK_S, 90 s) like a fresh one.
