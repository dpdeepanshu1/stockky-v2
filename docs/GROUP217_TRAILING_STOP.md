# Group 217 - trailing stop for Position Stocks (position-stocks-service)

Cumulative on group 216. Item 2 of the 2026-10-07 loss/profit review. Rebuild: `docker compose build position-stocks-service && docker compose up -d`.
Files changed: `orders/trailing.py` (new), `config.py` (7 settings), `main.py` (one call in the fast-reconcile loop), `orders/reconcile.py` (3-line guard),
`tests/test_trailing.py` (new, 32), `tests/test_reconcile.py` (+2).

## What the review found
Both Super Order placements send `trailing_jump=0.0`, and the breakeven move (DB toggle, OFF by default) only moves the stop once, to entry + 2 ticks.
A winner that ran +1.5% and fell back gave the whole gain up, while losers ran to the stop. Average win +0.3% against average loss -1.3% on 2 Oct.

## Change
`orders/trailing.py::run_trailing_stop` runs every fast-reconcile pass, after excursion tracking (which keeps `max_price_seen` current).
For each OPEN position with a Super Order (carried CNC positions skipped), once the peak is `TRAIL_ACTIVATE_PCT` above entry, the STOP_LOSS_LEG moves to
`peak * (1 - distance)`, `distance = max(TRAIL_MIN_DISTANCE_PCT, adaptive_stop_pct * TRAIL_DISTANCE_STOP_FRACTION)`.

Rules (pure function `compute_trailing_stop`, all unit-tested):
- the stop only goes up, and only by at least `max(tick, TRAIL_MIN_STEP_PCT of entry)` per modify;
- once active it is never below entry + `BREAKEVEN_STOP_BUFFER_TICKS` ticks, so a trailed trade cannot finish as a loss;
- always strictly below the live price (Dhan rejects a stop at/above the market); if the price already fell through the trail level the stop goes one
  tick under the market, which means "exit now";
- a stop at/above the target is not set; the TARGET_LEG is never touched; no new order is ever placed.

Interactions:
- A successful trail sets `stop_moved_to_breakeven = True`, so `orders/breakeven.py` can never later pull the stop back down to entry.
- `reconcile._apply_entry_correction` re-arms the legs from the real fill. It used to recompute the stop from the entry and could lower a ratcheted stop; it
  now keeps the higher of the two when the stop was already moved.
- A rejected modify (leg no longer pending, etc.) leaves the row unchanged and backs that position off for `TRAIL_RETRY_BACKOFF_S`. Throttles are in-process.
- First trail on a position sends one Telegram notice; later moves only log (`TRAILING_STOP:` lines).

## Settings (env, no DB toggle, no schema change)
| Env | Default | Meaning |
|---|---|---|
| `TRAILING_STOP_ENABLED` | 1 | `0` turns the whole thing off |
| `TRAIL_ACTIVATE_PCT` | 1.0 | peak gain vs entry before trailing starts |
| `TRAIL_DISTANCE_STOP_FRACTION` | 0.6 | trail distance as a fraction of the position's own stop % |
| `TRAIL_MIN_DISTANCE_PCT` | 0.4 | trail never tighter than this below the peak |
| `TRAIL_MIN_STEP_PCT` | 0.15 | minimum improvement (% of entry) before a modify is sent |
| `TRAIL_MIN_INTERVAL_S` | 20 | per-position minimum seconds between modify attempts |
| `TRAIL_RETRY_BACKOFF_S` | 60 | wait after a rejected modify |

Worked example (entry 100, stop 1%, peak 102): distance 0.6%, stop moves to 101.39, locking +1.39%. If the peak reaches 103 the stop follows to 102.38.

## Trade-offs and limits
- **It is ON by default and changes live REAL behaviour.** Set `TRAILING_STOP_ENABLED=0` before the rebuild if you want to start it later.
- Scalp targets are about 1.4-3.5%, so with activation at +1% the trail only helps in the last 0.4-2.5% before the target. It mainly protects the
  trades with the wider targets; a trade with a 1.4% target will mostly hit the target or stop as before. Lower `TRAIL_ACTIVATE_PCT` (for example 0.7) for more
  coverage, at the cost of more stop-outs on normal pullbacks.
- The peak comes from the in-memory tick buffer. After a service restart the stored `max_price_seen` is used until fresh ticks arrive.
- The Dhan modify of STOP_LOSS_LEG is the same call reconcile's re-arm already makes. The breakeven move (which would also have exercised it) has been OFF by
  default, so watch the first live `TRAILING_STOP:` lines for rejections (`rejected (...)`).
- Not done: a partial exit like real-trade-service has, and any change to `trailing_jump` on the Super Order itself.

## Tests
- `tests/test_trailing.py` (32): the stop formula and every clamp, throttle, back-off, switches, skipped positions, one bad symbol, notifier failure, breakeven
  cannot lower a trailed stop.
- `tests/test_reconcile.py` (+2): a late fill correction never lowers a ratcheted stop; an un-ratcheted stop is still re-armed as before.
- Run in the sandbox: `test_trailing.py`, `test_reconcile.py`, `test_breakeven.py` pass (251; trailing.py 95% covered). Whole position-stocks suite: 2777 passed, 2 failed. The same 2
  fail on the untouched group 216 zip: `test_group192...::test_two_dead_entries_today_block_for_the_rest_of_the_day` (time-of-day dependent, fails shortly after
  IST midnight) and `test_group210_symbol_lock_sweep::test_throttled_to_one_pass_per_interval`. Not changed here.
