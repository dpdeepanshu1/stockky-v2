# E214 - real-trade exit: a profit-target partial sale is not fired on an old price

**Where:** services/real-trade-service exit_engine/exit.py (`_stale_tick_blocks_target_sale`, target branch), config.py, control/catalog.py.
**Bug:** the priority price lane serves a last-good tick up to FEED_PRIORITY_STALE_FALLBACK_S (90 s) old (source `stale_last_good(...)`); the target check treated it as live and could sell 60% on it.
**Fix:** the target partial waits (HOLD row, next cycle decides) when the tick is a served last-good one, or older than `EXIT_TARGET_MAX_TICK_AGE_S` (30) when its age is known. Stop, emergency-gap and time-stop checks still act on any tick. Switch `EXIT_TARGET_STALE_GUARD_ENABLED` (default true, live on the Calibration page); false restores the old behaviour.
**Tests:** tests/test_e214_stale_tick_no_target_partial.py (8). The 5 helper tests were run here as plain asserts; the 3 cycle tests need pytest + SQLAlchemy (not installable here) - NOT RUN. docs/ENV_REFERENCE.md regenerated, `--check` passes.
