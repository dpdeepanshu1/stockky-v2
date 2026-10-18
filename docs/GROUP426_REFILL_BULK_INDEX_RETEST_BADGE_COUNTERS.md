# Group 426: refill honesty, bulk feed covers the whole index, sniper retest badge data, structure event counters

> Re-applied as group432 on the group 429/431 base (the group 429 zip did not contain these changes).

Four small changes from the 18 Oct review of the group 425 zip. No trading decision changes.

## 1. Refill Additional Data (`api-gateway/refill_additional.py`)
- **Fundamentals failures were invisible.** A run in which every fundamentals call failed still ended "587/587 ok, 0 errors" (the
  technicals / events were saved and the failed call was logged at debug level). `_hydrate_one` now tags the row with an internal
  `_fund_failed` key, the runner strips it before storing and counts it. Progress reads `(ok=3 err=0 fund_missing=3)`, the final message
  adds `; fundamentals missing for N`, the job gets a `fund_missing` field and one WARNING is logged. The symbol still counts as ok
  (its technicals were saved), so existing success semantics are unchanged.
- **Price source label is no longer overwritten.** For a symbol whose stored row already has a price (`last_price` / `current_price` / `price`),
  the refill row no longer carries `source`, so `nse_bhavcopy` / `yahoo_bulk` is not relabelled `refill_additional` (the price was
  not written by this job). One bulk read (`get_symbols_bulk`); any failure falls back to the old behaviour.

## 2. Bulk feed covers the feed index (`api-gateway/main.py`, `start_bulk_feed`)
The scan universe (~500 symbols, sometimes a stale copy) was smaller than the feed index (587), so about 48 indexed symbols (MARUTI, ONGC,
HAL ...) were never priced by this job. The indexed symbols are now appended after the universe (order kept, duplicates removed by the
existing cleaning). `BULK_FEED_INCLUDE_INDEX=0` restores the old list. Index read failure keeps the universe list.

## 3. Buy Sniper retest badge (`api-gateway/buy_sniper.py`)
`BuySniperModal` already renders `retestBadge(s.structure)` but the card's `structure` block never carried `retest_entry`, so the badge could not
show. `card["structure"]["retest_entry"]` is now copied from the decision's `structure` block on the scan row (zone names, or None). Display
only; nothing about the verdict changes. The branch itself (`STRUCTURE_RETEST_ENTRY`) is still OFF by default.

## 4. Structure event counters (`analysis-intelligence-service/technical/main.py`)
`/health` -> `structure` gains `bos`, `choch`, `sweep`, `fvg`, `ob`: how many freshly computed blocks carried each event (cache hits are not
counted). `_count_structure_events` never raises. The plan's `structure_rejections` / `rr_rejections` / `chase_rejections` live in the
decision service and are not part of this change.

## 5. Two pre-existing test failures fixed (they failed identically on the group 425 zip)
- `tests/test_group361_sniper_observation.py` (19 tests): the fixture summary used a fixed `ms_asof` of 2026-10-13; once the real date was 5 days later the
  sniper gate treated it as stale (`no_data`) and stopped rejecting. The fixture date is now yesterday on the real IST clock.
- `services/kronos-forecast-service/.dockerignore` was missing (4 `test_dockerignore.py` tests); added (same rules as the other services).

## Tests
New: `api-gateway/tests/test_group426_retest_badge_plumb.py`, `analysis-intelligence-service/tests/test_group426_structure_event_counters.py`,
`TestGroup426Refill` in `tests/test_refill_additional.py`, three bulk-feed tests in `tests/test_main_feed_control_audit.py`. Five old refill assertions
now include the `fund_missing` text (their fake upstreams return no fundamentals).
Result: api-gateway 8,896 passed (incl. the 23 formerly failing); analysis-intelligence-service files each pass.

## Not done (needs your VM / data, unchanged)
Evidence gates G1-G4, calibration run, the `.env` STRUCTURE_ grep, `npm run build`, and the other items in `REMAINING_TASKS.md`.
