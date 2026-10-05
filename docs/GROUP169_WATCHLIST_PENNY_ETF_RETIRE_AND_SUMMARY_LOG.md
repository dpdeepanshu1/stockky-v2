# Group 169 - watchlist: penny/ETF rows retired, INFO lines folded into one summary (real-trade-service)

Cumulative on group 168 (the merge of the 164 and 167 zips). Item 5, second part, of the second open-market log list.
Rebuild real-trade-service.

## What was wrong
- **Penny/ETF rows stayed active.** Group 157 only held them back, so every cycle each row was priced again and
  counted as `adverse`, until it expired days later.
- **About 450 INFO lines per cycle.** Each held-back row logged one `SKIPPED` line, throttled to once per 30 minutes
  per row. The throttle is in memory, so after a boot, and again every 30 minutes, all rows logged in the same cycle.

## The fix
- **ETF names** (built-in list, `*BEES`, `*ETF`, `WATCHLIST_ETF_SYMBOLS`): the row is retired (`expired`, reason
  `instrument: ...`) before any price lookup. `refresh_watchlist` no longer inserts them (one INFO line with the count).
- **Price below the penny floor** (`CANDIDATE_MIN_STOCK_PRICE`, default 20): the row is retired on the first priced
  cycle. `refresh_watchlist` does not re-add that symbol+catalyst for `WATCHLIST_DROP_COOLDOWN_HOURS` (default 24;
  the group 158 cooldown now also matches `instrument:` reasons).
- **Logging:** the per-row `SKIPPED` and `EXPIRED` lines are now ONE INFO line per cycle each, listing the first 8
  names plus a count (`... +N more`). The per-row line is DEBUG. The 30-minute per-row throttle still decides which
  held-back rows appear.
- **Tally:** existing keys unchanged; `instrument_expired` appears only when non-zero. Penny retirements also count
  in `adverse`, as before.
- **Switches:** `WATCHLIST_INSTRUMENT_RETIRE=0` restores group 157 (held back, stays active, still one summary line).
  `WATCHLIST_ADVERSE_GUARD=0` turns off all the guards as before.

## Things to check
- A stock that is under Rs 20 today and rises above it later stays out for 24 hours, then can be added again.
- The ETF test is by name only, as in group 157. If an ETF slips through, add it to `WATCHLIST_ETF_SYMBOLS`.
- If a stock has several active rows, the primary row is retired first and the others follow on the next cycles.
- The `/watchlist` dashboard will show many more `expired` rows with reason `instrument: ...` right after deploy.

## Tests
New `tests/test_group169_watchlist_instrument_retire_summary_log.py` (19 tests). `test_group157_...` now pins
`WATCHLIST_INSTRUMENT_RETIRE=0` so it keeps covering the hold-back path.
Not run here: the sandbox has no pytest or sqlalchemy. The new helpers (summary line, retire switch, throttle,
instrument reason) were run standalone and behaved as expected.

    python3 -m pytest services/real-trade-service/tests/test_group169_watchlist_instrument_retire_summary_log.py services/real-trade-service/tests/test_group157_watchlist_penny_etf_hold.py services/real-trade-service/tests/test_group155_watchlist_adverse_guard.py services/real-trade-service/tests/test_group158_watchlist_deep_drop_expiry.py services/real-trade-service/tests/test_group164_watchlist_one_row_per_symbol_index_filter.py services/real-trade-service/tests/test_watchlist_trigger.py -q
