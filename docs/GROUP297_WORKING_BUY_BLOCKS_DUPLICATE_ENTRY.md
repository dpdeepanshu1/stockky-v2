# Group 297 - a BUY that is still working at the broker blocks a second BUY of the same symbol (real-trade-service)

Result of the audit of the other entry paths for the held-symbol gap fixed in group 296.

## What was found
The no-pyramiding check (`risk_engine` check 7) tests the symbol against `open_position_symbols`, which comes from
`held_exposure_positions()` - positions only. A position exists only after a fill is booked. While a BUY was still PLACED or
PARTIAL at the broker the symbol was invisible to the check, so these could all pass it for the same symbol:
- a candidate from a later cycle (the per-cycle same-symbol guard in `evaluate_mode` only covers ONE batch);
- a manual Confirm BUY;
- a candidate from another source (standard / watchlist / manual).
The cross-service symbol lock does not help: it is re-entrant for the same service ("already ours").

## What changed
- `portfolio.working_buy_symbols(db, mode)`: symbols with a BUY order of the mode in status PLACED or PARTIAL, created within
  `ENTRY_WORKING_BUY_MAX_AGE_HOURS` (default 24, blank/invalid/<=0 = 24, so an order nobody expired cannot block a symbol for
  good). Fails open (empty set) on any error.
- Added to `open_position_symbols` in the three places that build the account state: `entry_engine/entry.py::_account_state`,
  `manual_engine.py::_account_state`, and the `/risk-engine/check` dry run in `main.py`. `open_position_count` and the risk
  figures are unchanged (an unfilled order is not a position).
- Only applies while pyramiding is off for the mode (the check's own condition).
- The rejection text now says "already has an open position or a BUY order still working".
- `ENTRY_BLOCK_WORKING_BUY_DUP=0` restores the old behaviour. Both settings are in `.env.example` and `.env.oracle.recommended`.

## What it does not do
- A symbol blocked this way is rejected as `no_pyramiding` like a held one; once the order fills, expires or is rejected the
  normal rules apply again. An order that sits PLACED with no `valid_until` blocks the symbol up to the age limit.
- I did not find the incident this audit started from (AURIONPRO); this closes the gap the code showed.

## Tests
`tests/test_group297_working_buy_blocks_duplicate.py` (22): statuses, SELL / other mode / old orders, age limit and parsing,
switch values, fail-open, entry and manual account state, the real risk engine rejecting the second buy (and not another symbol,
not with pyramiding on, not after expiry), and the dry-run route. 13 mutations on the new logic, all caught (one needed the route
test first).

Full real-trade suite: **4202 passed, 1 skipped** (4180 + 22), Python 3.12. Other services unchanged in this group.
