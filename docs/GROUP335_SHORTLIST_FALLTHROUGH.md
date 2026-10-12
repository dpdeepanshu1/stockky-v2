# Group 335 - the best-3-to-5 shortlist falls through (2026-10-12)

**Audit result.** The shortlist from group 332 ranked the picks correctly, but entry did not use them as a list:

| Service | What happened before | Effect |
|---|---|---|
| position-stocks | `main.py` tried the shortlist best-first but stopped at the first skip that was not "insufficient capital". Every other gate (VWAP, liquidity, depth, circuit band, pre-open gap, re-entry guard, opening gate, price guard, board chase, cost gate) returns `None` inside `attempt_entry`. | One refused pick ended the cycle; picks #2-#5 were never tried. The two gates added in group 333/334 made this far more likely. |
| real-trade | Gate 6 selected the first `ENTRY_MAX_NEW_PER_CYCLE` of the shortlist; the rest were marked WAIT before any order was sent. | If the best pick's Dhan order failed, the next pick waited a whole cycle. |

Checked and fine: position-stocks quality-checks all `max(QUALITY_GATE_TOP_N, SHORTLIST_MAX)` ranked names concurrently, so the shortlist really compares all passers; real-trade reads the 20 highest-conviction rows (+5 oldest), nudges the composite from the Market Board (max +/-6, ranking only) and logs the shortlist each cycle. Manual BUYs deliberately bypass the shortlist.

## Fix
- **position-stocks.** `orders/entry.py` remembers the reason of the last SKIPPED row per thread. `is_symbol_specific_skip()` says whether it concerns only that symbol (prefixes: PENNY_STOCK, RANGE_GATE, OPENING_GATE, PRICE_GUARD, ENTRY_REJECTED_TODAY, REENTRY_, ALREADY_OPEN_HERE, SYMBOL_HELD_BY_OTHER_SERVICE, DEPTH_, CIRCUIT_, PREOPEN_GAP, VWAP_, LIQUIDITY_, BOARD_CHASE, COST_GATE, SECURITY_NOT_FOUND, ENTRY_ORDER_FAILED_COOLDOWN). If so the cycle logs `skipped (...) - trying next shortlisted pick` and tries the next one. Anything else (not armed, kill switch, order budget, max positions, margin pause, shadow window) still ends the cycle. `SHORTLIST_FALLTHROUGH=0` restores stop-at-first-skip. The cycle summary gains `shortlist_skips` when no pick cleared.
- **real-trade.** Shortlist picks beyond `ENTRY_MAX_NEW_PER_CYCLE` are kept in reserve. When a selected pick's REAL broker placement fails for a reason about that symbol (Dhan rejection, circuit-limit rejection, symbol held by the other service) the next reserve pick is placed in the freed slot, never exceeding the per-cycle limit. Not promoted: invalid-IP (cycle already returns and disarms) and the shared order budget being used up (every pick would hit it). Unused reserve picks are marked WAIT with the same text as before, after the placement loop. `ENTRY_SHORTLIST_FALLTHROUGH=false` restores the old behaviour.
- Nothing is added to the candidate list and no gate is lowered: every promoted pick already passed every earlier gate and risk approval.

## Tests
`tests/test_group335_shortlist_fallthrough.py` in both services. position-stocks: reason classification table, per-thread reason, wrapper using a patched `attempt_entry`. real-trade: failed best pick hands its slot to the next, shared-budget failure does not promote, flag off = old behaviour, slot limit never exceeded.

## Not verified here
The sandbox has no pytest, sqlalchemy or fastapi. The skip-reason helper block was run directly; the test files are only syntax-checked. Run `bash run_tests.sh` on the VM. If an older real-trade Gate 6 test indexes `entry_details` by position when more than `ENTRY_MAX_NEW_PER_CYCLE` candidates are approved, the reserve WAIT rows now come after the entered rows (they used to come before).

## Logs
No log was attached to this round, so nothing was checked against a real run. After deploying, look for: `trying next shortlisted pick` / `no shortlisted pick cleared its entry gates` (position-stocks) and `promoting next shortlisted pick` (real-trade). Paste the next boot log and the market-hours log and the remaining failures will be listed from it.
