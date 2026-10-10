# Group 298 - the automatic scalp entry refuses a symbol this service already holds (position-stocks-service)

Result of the audit of the scalp-pool entry path (the follow-up to groups 296 and 297).

## What was audited
- A buy here is not "working but invisible": `attempt_entry` creates the `ScalpPosition` row (status OPEN) in the same call, so
  there is no unfilled-order window like the one closed in real-trade-service by group 297.
- The cross-service `try_claim()` lets a symbol this service already holds through ("already ours"), so it is no guard here.
- `attempt_manual_entry` has had an explicit "already has an open position here" check since 2026-09-18.
- `attempt_entry` (automatic) had none of its own. It relied on the scan excluding open symbols, but that set is a snapshot taken at
  the start of the cycle (`main.py::_run_cycle`, under the cycle lock). A manual BUY runs outside that lock, so a symbol opened
  between the snapshot and the entry attempt could be bought a second time (two rows, two slots of the position cap).

## What changed
`orders/entry.py::attempt_entry`: after the slot-cap check and before the symbol lock is claimed, a live query for an OPEN or
EXIT_LEGS_REJECTED position of the same symbol. If one exists the candidate is logged SKIPPED with
`ALREADY_OPEN_HERE:id=<id>,status=<status>` and nothing else happens (no lock claimed, no capital reserved, no order). Same
statuses as the manual guard and the scan's exclusion set. No new setting (same as the manual guard).

## Not changed
- I did not find an incident for this in the repo; it closes the race the code showed. The window is small (one cycle), the
  existing slot cap still applies, and the broker's own order checks are unchanged.
- The scalp-pool credit "jump" check still needs real numbers.

## Tests
`tests/test_group298_auto_entry_skips_open_symbol.py` (9): both held statuses skip cleanly (no position, no lock, no order,
capital untouched), finished statuses and other symbols do not block, the guard runs before the lock is claimed, and the slot-cap
message is unchanged. 5 mutations on the new check, all caught.

position-stocks full suite: **3207 passed** (3198 + 9). real-trade-service is unchanged in this group. Rebuild position-stocks-service.
