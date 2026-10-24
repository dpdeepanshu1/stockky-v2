# Short selling item 10: exit tests against the real exit code, and the missing 14:55 cover - E102

Position Stocks only. Longs untouched. `SHORT_MODE` is still `shadow` by default, so nothing live changes until you switch it.

## A real gap found while writing the tests, and fixed
`SHORT_SQUAREOFF_TIME_IST` (14:55) was read ONLY by the shadow shorts (`short_entry.resolve_open`). A LIVE short had no 14:55 cover: it would have been closed by the normal 15:00 sweep, although the entry alert says "Covered by 14:55 at the latest" and the T7 pass rule is "the position never exists after 14:55".
- NEW `orders/eod_squareoff.py::run_short_squareoff(db, now=None)`. At or after `SHORT_SQUAREOFF_TIME_IST` (read at call time, so a Switches-page change applies at once) it closes every OPEN or EXIT_LEGS_REJECTED short through `close_position_now(..., exit_reason="EOD_SQUAREOFF")`: cancel the legs, wait, plain MARKET BUY (INTRADAY), `EOD_SQUAREOFF_PENDING_RECONCILE`, then reconcile books the real fill. It places no order of its own, so the retry cooldown, the bracket-already-filled guard (no BUY after a stop or target already filled) and the CNC refusal all apply. Longs and rows with an unknown side are skipped. Before the time it does not even query. It never raises. A refused cover is logged once per reason and left OPEN; the 15:00 sweep is the second chance.
- `main.py`: called in the fast reconcile loop and in the trading cycle, both just before the 15:00 sweep check. No once-a-day lock (a short opened late, or after a restart, is still covered).

## Tests (item 10)
- NEW `scripts/test_short_exit_paths.py` (61 cases, stdlib only, no SQLAlchemy, no pytest, no network). It loads the real `eod_squareoff`, `reconcile`, `exit_retry`, `close_all`, `stop_watchdog`, `overnight_stop`, `side`, `statuses` and replaces only the edges: an in-memory database that really applies the filters, the ledger, the notifier, and a broker client whose error classifiers are lifted from the real `dhan_client.py` source (not rewritten). Covers: stop hit (price above entry, loss sign), target hit (price below entry, win sign, plus a long as a control), leg fill with no price (cover price comes from the BUY, never from the entry SELL), half-filled target leg and half-filled cover, timeouts then success (every attempt a BUY), a timed-out BUY that was in fact accepted (adopted, no duplicate), the short's own entry SELL never mistaken for the cover, circuit-limit and margin rejections, the cooldown and its alert, a dead cover (ERROR, capital and lock taken back), a duplicate rejected cover after a real fill, the 14:55 cover (time gate, BUY only, legs cancelled first, long untouched, no double cover, then the 15:00 sweep), restart cases (live legs, fill while down, cover sent but not booked, once-a-day guard, both legs dead), close-all with a short and a long, the stop watchdog (a short breaches UPWARD, never downward), and the re-arm of the legs after an entry-fill correction (target below, stop above).
- NEW `tests/test_short_exit_paths.py`: runs that script in a clean interpreter, plus static checks on the wiring.
- Mutation check (scratch copy): exit side forced to SELL, long-only P&L sign, sweep not skipping longs, no time gate, duplicate guard ignoring side. Each one makes the script fail.
- `stockky_full_test.sh` PART 0 runs the script. `scripts/test_short_live.py` (47) and `tests/test_short_exit_side.py` / `test_short_foundation.py` (27) still pass. `check_shared_copies`, `check_control_catalog` OK.

## NOT run here
pytest and SQLAlchemy cannot be installed in this workspace (pypi is blocked), so the position-stocks pytest suite was not run, and the real SQLAlchemy models and Dhan's real answers are not exercised. Still for the VM: `python3 -m pytest tests -q` in position-stocks-service, then T6 (Dhan SELL probe) and T7 (1-share live short, watch the 14:55 cover happen).

## Observed, not changed (affects longs too)
`reconcile._extract_leg_price`: when a filled exit leg has no price field AND the order book has no single matching exit order, the fallback chain reaches the PARENT row's `averageTradedPrice`, which is the ENTRY fill, so exit = entry and the trade books P&L 0 until the 5-minute repair (group344) corrects it. This is old behaviour for longs; a short hits it the same way. Worth a look together with P2 item 15 (stop overshoot).

## Still open
P2 items 13 to 18, the VM runs in P0 item 6, and T6 / T7.
