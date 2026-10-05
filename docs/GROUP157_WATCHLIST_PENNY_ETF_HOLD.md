# group157 (2026-10-05) - watchlist trigger: hold back ETFs and penny stocks

Cumulative on group156. Rebuild: `docker compose build real-trade-service && docker compose up -d`.

Source: item 3 of the remaining list after group156. Files changed: `real-trade-service/entry_engine/entry.py` (+ one new test file).

## What the log showed
MASPTOP50 and MAFANG (ETFs) and HARDWYN, JHS, UCOBANK (low-priced) were QUEUED by the watchlist trigger. Each queued row costs a
daily-history fetch in `evaluate_mode`, and downstream they are rejected anyway (the candidate engine has a Rs 20 price floor and
an ETF is not a stock this strategy trades), so the fetch was wasted rate-limit budget.

## Fix (entry.py)
Inside the existing `_watchlist_adverse_reason` guard, before the drop and Tier-3 checks:
- **Penny:** live price below `CANDIDATE_MIN_STOCK_PRICE` (the same env/default, 20, that `candidate_engine/candidates.py` uses) is not queued.
- **ETF:** the symbol is in a built-in list, ends in `BEES` or `ETF` or contains `ETF`, or is listed in `WATCHLIST_ETF_SYMBOLS`
  (comma separated, case-insensitive, `.NS`/`.BO` ignored).
- Same semantics as group155: the row stays `active`, nothing downstream changes, one INFO `SKIPPED (not queued, stays active)` line
  per symbol per 30 min, counted in the existing `adverse` tally (so no tally keys changed). `WATCHLIST_ADVERSE_GUARD=0` turns this
  off together with the other guards. Any exception inside the check is logged and the row is queued as before.

## Tests
New `tests/test_group157_watchlist_penny_etf_hold.py` (ETF names detected / normal names not, env extension, ETF row and penny row not queued and stay active, price exactly at the floor still queues, normal stock queues, floor follows env, bad env falls back to 20, off switch, helper never raises, log once per window).
No pytest/sqlalchemy in my sandbox, so the pytest file is compiled but not run here: the real helper functions were extracted from `entry.py` and checked directly (all pass). Run on the VM: `python3 -m pytest tests/test_group157_watchlist_penny_etf_hold.py tests/test_group155_watchlist_adverse_guard.py tests/test_watchlist_trigger.py -q`.
The existing group155/156 and watchlist-trigger tests only use prices of 90 and above and ordinary symbols, so they should be unaffected.

## Judgement calls to check
- The ETF test is by NAME only (a watchlist row has no instrument-type field). It will miss an ETF with an unusual name; add it to `WATCHLIST_ETF_SYMBOLS`. The built-in list is my guess at common NSE ETFs, not an exchange list.
- HARDWYN, JHS and UCOBANK are held back only if their price is under Rs 20. If one trades above that, it is not a penny stock by this floor and will queue as before.
- The hold is per cycle. A row for such a symbol stays active until it expires, so it is re-checked (price only, no history fetch) each cycle.

## Not changed
Items 4-13 of the list (symbol aliasing, dead-symbol retries, duplicate quote calls, data-gap logging, ...) and the unreachable site (VM side).
