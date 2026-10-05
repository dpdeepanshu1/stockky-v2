# group158 (2026-10-05) - watchlist: retire rows that fell far below their catalyst

Cumulative on group157. Rebuild: `docker compose build real-trade-service && docker compose up -d`.

Source: item 9 of the remaining list. Files changed: `real-trade-service/entry_engine/entry.py`,
`real-trade-service/watchlist_engine/watchlist.py` (+ one new test file).

## What the log showed
Watchlist rows 4-45% below their catalyst price were still `active` and re-checked every cycle, and about 30 new rows were
added each cycle.

## Cause (first half, fixed here)
Group155 made `evaluate_watchlist_entries` stop QUEUEING a row that had fallen more than 3%, but the row stayed `active` until
`expires_at` (3x the half-life: 36 days for "results", 30 for "board"). A stock that is 30% under its catalyst will not
be bought on that thesis, so the row only cost a quote lookup each cycle and kept the table growing.

## Fix
- `entry.py`: after the "ran past the band" check, a fall deeper than `WATCHLIST_EXPIRE_DROP_PCT` (default 0.15 = 15%) sets
  `status="expired"` and `missed_reason="adverse: fell -X% below catalyst (retire limit -15.0%) (catalyst Rs A, now Rs B)"`,
  logs one INFO `EXPIRED` line, and counts in the existing `adverse` tally (no tally keys changed). Falls between 3% and 15%
  behave as in group155 (not queued, stay active, can recover). `WATCHLIST_EXPIRE_DROP_PCT=0` or `WATCHLIST_ADVERSE_GUARD=0` turns
  retirement off. Bad env values fall back to 15%.
- `watchlist.py`: `refresh_watchlist` skips a symbol+catalyst type that was retired this way within
  `WATCHLIST_DROP_COOLDOWN_HOURS` (default 24, 0 = off). Without this the next source poll would re-insert it with the new,
  lower price as catalyst price (Tier 1/2 take the current price from the source; Tier 3 takes the first live tick) and the
  drop check would start again from zero. Rows expired for other reasons are not affected.

## Tests
New `tests/test_group158_watchlist_deep_drop_expiry.py` (deep drop expires and is not queued, -10% and exactly -15% stay active,
env override, 0 disables, bad env, off switch, second pass ignores the retired row, cooldown blocks re-add, re-add after cooldown,
cooldown 0, ordinary expired rows not blocked, per symbol and catalyst, env parsing).
No pytest/sqlalchemy in my sandbox, so the file is compiled but NOT run here. The new helper functions were extracted from the source
and checked directly (pass). Existing watchlist tests only use falls of 10% or less, so they should be unaffected. On the VM:
`python3 -m pytest tests/test_group158_watchlist_deep_drop_expiry.py tests/test_group157_watchlist_penny_etf_hold.py tests/test_group155_watchlist_adverse_guard.py tests/test_watchlist_trigger.py -q`

## Judgement calls to check
- 15% and 24 h are my picks, not measured. Look for `EXPIRED ... adverse:` lines for a few sessions; if a stock that recovered is
  being retired, raise `WATCHLIST_EXPIRE_DROP_PCT` (e.g. 0.25).
- Rows 3-15% under their catalyst still stay until natural expiry.

## Not changed
The "about 30 new rows each cycle" half. I could not tell its cause from the code alone: Tier 3 rows get a fresh `catalyst_ts` each
poll, so a Tier 3 row that goes `missed` (ran past its band) is re-inserted next cycle by design. If the growth continues, send a few
`trade_watchlist` rows (symbol, catalyst_type, status, created_at) and I will look at which status they leave `active` with.
Items 4-8, 10-13 of the list and the unreachable site (VM side).
