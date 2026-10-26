# E206 - a manual BUY with a target at or below the entry is rejected

**Where:** real-trade-service/manual_engine.py, `evaluate_manual_order` BUY branch (follows the existing stop-below-entry check). Open item from E195.
**Bug:** a typed target <= entry was accepted: risk-reward showed negative and a target-hit exit would sell at a loss.
**Fix:** `reason="invalid_target"`, "Target price must be above entry price." Only a target the caller supplied is checked (the fallback target is always above entry); the reference is the (tick-rounded) limit price when given. The swing caller (`swing=...`, passes no target) is unaffected.
**Tests:** tests/test_e206_manual_buy_target_must_exceed_entry.py (6 cases). NOT RUN (no pytest/SQLAlchemy here); py_compile only.
