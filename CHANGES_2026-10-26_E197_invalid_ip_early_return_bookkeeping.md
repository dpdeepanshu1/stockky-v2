# E197 - invalid-IP early return no longer drops the remaining picks

**Where:** real-trade-service/entry_engine/entry.py, evaluate_mode, the Dhan "invalid IP" placement branch.

**Bug:** when a BUY was rejected for an unwhitelisted IP, evaluate_mode auto-disarmed REAL and returned early. That return skipped the end-of-cycle bookkeeping. The reserve picks and any selected pick not yet reached had already been consumed, so they vanished with no decision row. The Board shortlist panel kept their outcome blank.

**Fix:** before the early return, mark the failed pick PLACEMENT_FAILED, write WAIT decision rows for the unreached picks (with a note that REAL was auto-paused), close the shortlist, and commit. This is wrapped in try/except so it can never block the auto-disarm.

**Not a money bug:** no order or capital was affected. This is an audit and visibility fix.

**Tests:** tests/test_e197_invalid_ip_early_return_bookkeeping.py (2). Both fail on the E196 code and pass now. The real-trade suite passes apart from the sandbox-only oracledb failures.
