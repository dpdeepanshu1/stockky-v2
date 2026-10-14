# group358 - dynamic universe add/drop contradiction (log review item #4)

Source: the 14 Oct 10:38 IST restart log, item 4 (High) of the pasted log-issue list.

## What the log showed
`dynamic_universe: added 60 symbols: [...]` and `dynamic_universe: dropped 31 inactive symbols: [...]` in the same sync, listing the
SAME names (ADVANCE, AEROFLEX, ANUHPHR, ARVEE, CGVAK, ECORECO, EIMCOELECO, FAZE3Q, GERMAN, HALDYNGL, HILINFRA, HMAAGRO, HTEL ...).
Added names were bare, dropped names carried `.NS`.

## Cause (read from the code, both services)
- analysis-intelligence-service `event/main.py` `_normalize()` stores every subscription as `SYM.NS`, and `GET /subscriptions` returns
  that spelling.
- real-trade-service `watchlist_engine/dynamic_universe.py` builds the desired set from the scanners as bare `SYM` and compared the two
  sets as raw strings (`set(desired) - current_auto`, `current_auto - set(desired)`). They could never overlap, so every sync
  "added" the whole desired set and "dropped" the whole current auto set, and `kept` was always 0.
- Effect: each 20-minute sync unsubscribed then re-subscribed the same symbols. `/unsubscribe` clears the symbol's `last_known`
  snapshot, so the next `/check` saw fresh "changes" (likely part of item 9, the same symbols queued every cycle) and the event
  tracker's subscription list flapped. The log's first sync after restart also showed the 60 adds with nothing kept.

## Change
`real-trade-service/watchlist_engine/dynamic_universe.py`: new pure `_diff_universe(desired, current_auto)` comparing on the bare
symbol (`_base_symbol`: upper-case, `.NS` / `.BO` stripped). `to_add` stays bare (what `/subscribe` expects), `to_remove` keeps the stored
spelling (a `.BO` subscription is unsubscribed as `.BO`), `kept` counts real overlap. Return shape of `refresh_dynamic_universe` is
unchanged. Only the diff changed; subscribe / unsubscribe / `/check` calls are as before.

Switch: `DYNAMIC_UNIVERSE_DIFF_NORMALIZE` (default 1; 0/false/off/no = old raw diff). In `.env.example` and `.env.oracle.recommended`.

## Expected in the next log
`dynamic_universe: added N` only for names not already subscribed, `dropped` only for names that left the scanners, and no name in
both lists. After the first sync most syncs should add / drop single digits.

## Tests
`tests/test_watchlist_dynamic_universe.py` (real-trade): 3 end-to-end cases (suffixed subscriptions match bare desired, only real
changes move, switch off) and `TestDiffUniverse` (7 pure cases, including the log's names and a `.BO` subscription).

## Also in this zip
Two real-trade tests failed on the untouched group 356b zip (same two on the original): `test_group164 ... test_no_flapping_across_cycles` and
`test_group344_log_review.py::test_in_band_row_is_never_parked`. Cause: group345's 300 s requeue cooldown parks a QUEUED row, so the
second pass in each test skipped the quote call. Both tests now set `WATCHLIST_REQUEUE_COOLDOWN_S=0` (test bugs, no code change).
`docs/ENV_REFERENCE.md` regenerated. Full real-trade suite: 4544 passed, 1 skipped, plus those two now fixed.

## Not changed
- "Dropped symbols are still fetched and traded" (GERMAN `QUEUED`): this module never removed GERMAN for real; it was the churn above.
  Nothing else in the repo reads the auto subscription list to gate trading, so no second change was made. If GERMAN still queues
  with `move=0.00%` it is item 10 (stale price gate), not this.
- Items 2, 3, 5-19 of the list remain. Next by priority: item 3 (api-gateway sync `oracledb` rollback blocking the event loop) or item 10.
