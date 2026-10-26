# E188 - market-data: /quotes/bulk stamped old ticks as fresh, left out the previous close, and had no deadline on its AngelOne stage

Includes E187 (and E186, E185, E184).

## What I read
`services/market-data-service/main.py`: `get_quotes_bulk` (the wrapper) and `_get_quotes_bulk_core` from the top to the end (cache check, closed-market
last close, Dhan stage, live-feed stage, AngelOne REST stage, pre-open, cooldown and small-leftover gates, yfinance rate-limit peek, `yf.download`, row
extraction), plus the helpers it calls (`_pad_quote_response`, `_restamp_quote`, `_cooldown_stale_row`, `_leftover_stale_row`, `_angelone_rest_quote_first`),
`angelone_client.get_quotes_batch`, `angelone_budget.admit` and the two live feeds' `get_live_quote`. Callers: real-trade-service `market_feed/feed.py`
(chunks of 100, 12 s timeout) and `candidate_engine/candidates.py`.

## Findings (fixed, all in `_get_quotes_bulk_core`)
1. **A live-feed tick was stamped with the time of the read.** The stage accepts a tick up to 15 s old and set `fetched_at = now`, so a 14 s old price came back
   as 0 s old (`age_s` 0) and was then cached for 15 s more under that stamp: a price up to ~30 s old answered as fresh. real-trade-service rejects bulk rows older
   than 20 s by `fetched_at`, so it could not catch this. Both feeds already carry the tick's own epoch time in `ts`; the row now carries it (never in the
   future; `now` only when `ts` is missing or unusable). A cached live row therefore stops counting as fresh once the tick itself is `BULK_CACHE_MAX_AGE_SEC` old
   and is refreshed instead. `QUOTE_BULK_LIVE_TICK_TS=0` restores the old stamping.
2. **Rows from the live feeds and from AngelOne REST had no `previous_close` / `day_change_pct`.** The single `/quote` path always set them (AngelOne's `close` is the
   previous session's close; Yahoo's feed sends `previous_close` / `day_change_pct`); the bulk path dropped them, so a caller reading bulk saw "no previous close"
   and fell back to other calls. They are filled when the source has a positive previous close and are never invented or set to 0. A row built from a feed tick whose
   source is neither AngelOne nor Yahoo does not treat its `close` as the previous close. No setting.
3. **The AngelOne REST stage had no overall deadline.** Each 50-token chunk may wait up to `ANGELONE_LANE_MAX_WAIT_S` (20 s) for a rate token, so a 100-symbol call
   (the caller's chunk size) could hold a worker thread for ~40 s after its caller gave up at 12 s; under load those threads pile up in market-data. The stage now has
   one wall-clock budget, `QUOTE_BULK_ANGELONE_DEADLINE_S` (default 10 s, 0 = off); a chunk still waiting at the deadline is cancelled, later chunks are not asked, the
   unpriced symbols continue through the existing stages (cooldown stale rows, small-leftover gate, yfinance peek) and one INFO line says how many were left.

## Behaviour to expect
- When the feed polls slowly, more bulk calls re-read symbols through AngelOne REST than before (their cached rows now age by the tick, not by the read). That is the
  honest age; the existing cooldown / budget gates still bound the load. If it shows in the budget counters, `QUOTE_BULK_LIVE_TICK_TS=0` is the way back.
- A normal fast AngelOne stage (well under 10 s) is unchanged.

## Read and found fine (no change)
- Order of the stages, the closed-market and pre-open last-close answers, the Dhan stage placement, the stale-row fallback in `get_quotes_bulk` (a stale row is only
  added for a symbol nothing else priced, and keeps its real `fetched_at`), the yfinance rate-limit peek, and the `yf.download` extraction (single / multi ticker frames).
- Not changed, same pattern: the single `GET /quote/{symbol}` live-feed branches (`get_quote`, two places) also stamp `fetched_at = now` for a feed tick. Left for a later
  round; the helper `_live_tick_fetched_at` can be reused there.

## Also
- `docs/ENV_REFERENCE.md` regenerated: it was already out of date before this round (line numbers moved by E183-E187 in position-stocks), which would fail the CI step.
  `python3 scripts/gen_env_reference.py --check` passes now.

## Tests
`tests/test_e188_bulk_quote_freshness.py`, 20 tests. pytest is not installed in the sandbox, so they were run through a small stand-in runner with stub modules for the
missing libraries: 20/20 pass on the changed code; against the unchanged E187 `main.py` 15 of them fail (the 5 that pass there are the "unchanged behaviour" checks).
Existing `test_group193`, `test_group244`, `test_group233`, `test_group235`, `test_group270`, `test_group487` give identical results before and after under the same
runner (some cases cannot run under it, identically on both). The real suite needs the VM:

    bash scripts/docker_test.sh market-data-service
    python3 scripts/gen_env_reference.py --check
