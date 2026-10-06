# Group 216 - entry slippage tolerance halved (position-stocks-service)

Cumulative on group 215. Item 3 of the 2026-10-07 loss/profit review. Rebuild: `docker compose build position-stocks-service && docker compose up -d`.
Files changed: `config.py` (two defaults), `tests/test_scalp_review_20261005.py` (default pins), `tests/test_entry.py` (+2 boundary tests).

## What the review found
The scalp BUY is a MARKET order sent seconds after the scan tick. `_price_guard_reject` re-reads the live tick and rejects only when it is
more than `ENTRY_MAX_SLIPPAGE_PCT` above the signal price. That was 0.5%, but the adaptive stop is 0.8-2.0%, so one entry could give away up
to ~60% of the tightest stop before the trade even started. The alert for a bad real fill (`ENTRY_FILL_SLIPPAGE_ALERT_PCT`) was 1.0%, so a
fill that cost half a stop was silent.

## Correction to the review (what was already right)
The review said stop and target are built from the signal price, not the fill. That is only true at placement. `reconcile._apply_entry_correction`
(2026-09-18) already re-arms TARGET_LEG and STOP_LOSS_LEG from Dhan's real average fill once reconcile sees it. So no change was needed there;
the remaining loss is the slippage itself, plus the short gap before reconcile runs.

## Change
- `ENTRY_MAX_SLIPPAGE_PCT` default 0.5 -> 0.25.
- `ENTRY_FILL_SLIPPAGE_ALERT_PCT` default 1.0 -> 0.5.
- Both stay env-overridable; `ENTRY_MAX_SLIPPAGE_PCT=0.5` restores the old tolerance, `0` disables the check. No code logic changed.

## Trade-off
A fast mover that ticks more than 0.25% above its signal in the seconds before the order is now skipped instead of bought. Those are the
chase entries, so some real winners will also be missed. Watch the `ENTRY_SLIPPAGE` SKIPPED lines for a few sessions.

## Not changed
- The guard compares the last traded price, not the ask, so a MARKET buy can still fill a spread above it.
- Stop-relative tolerance (a fraction of the actual stop) would need `compute_levels` to run before the guard. Not done.

## Tests
- `tests/test_scalp_review_20261005.py`: default pins now 0.25 / 0.5.
- `tests/test_entry.py::TestScalpReviewGuards`: +2 (0.30% rejected, 0.20% and exactly 0.25% pass; env 0.5 restores old behaviour).
- Sandbox has no pytest/sqlalchemy: config defaults were read from the real module, the boundary maths was checked by hand, all changed files
  compile. Run `bash run_tests.sh` on the VM.
