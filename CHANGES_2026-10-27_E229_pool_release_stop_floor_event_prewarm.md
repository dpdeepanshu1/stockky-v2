# E229 (2026-10-27) - DB connection released before network waits, stop-vs-ATR floor all day, event pre-warm, /surprise/audit off the event loop

From the 27 Oct log review. Four small fixes.

1. **api-gateway `/surprise/audit`**: `audit_surprise_feed()` (sync, reads the KV cache over the network) now runs through `asyncio.to_thread`. Fixes the logged `event loop blocked for 3.1s`. Test: `services/api-gateway/tests/test_e229_surprise_audit_off_event_loop.py`.
2. **position-stocks DB pool**: `_run_cycle` calls `_release_db_connection()` (commit, never raises) before the market-gate fetch and before the quality-gate gather, so the Session no longer holds one of the 4+4 pool slots through multi-second network waits (`QueuePool limit of size 4 overflow 4 reached` at the open). When the pool runs dry the loop now logs `pool size / checked_out / overflow`.
3. **Stop inside the noise**: `opening_gate.stop_reject` also applies after the opening window (was 09:15-10:00 only). Stop % must be >= `OPENING_GATE_MIN_STOP_ATR_FRAC` (0.3) x daily ATR %. Reject reason after the window: `STOP_ATR_FLOOR:STOP_TOO_TIGHT:...`. Missing ATR after the window FAILS OPEN (inside the window it still fails closed). Switch: `STOP_ATR_FLOOR_AFTER_WINDOW` (default true; false restores the old behaviour). A name with a 7.76% ATR and the 2% stop cap is now skipped.
4. **Event check pre-warm**: `quality_gate.prewarm_events()` starts the shared one-per-symbol event request in the background for the `QUALITY_GATE_EVENT_PREWARM_EXTRA` (default 3, 0 = off) names ranked just below the quality-gate batch, so a shortlist fall-through finds the answer in the late-answer cache. Never waits, never raises.

Tests: `services/position-stocks-service/tests/test_e229_release_connection_and_stop_floor.py`, `test_e229_event_prewarm.py`; `test_group268_opening_gate.py::test_stop_reject` updated for the new after-window behaviour.

Reviewed, no change needed: real-trade already has a general late-entry cut-off (`ENTRY_LAST_TIME_IST`, group 470); dead news feeds already fall back to Google News and are marked dead (groups 241 / 346); market-data 403 / rate-limit handling is already layered (groups 101, 127, 211, 225, 251).
