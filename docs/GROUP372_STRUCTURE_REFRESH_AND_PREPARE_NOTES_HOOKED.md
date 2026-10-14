# Group 372 - structure refresh job and PREPARE notes hooked into the gateway

The previous session was cut off. `structure_refresh.py` (group 360) and the PREPARE-note tests (group 358) existed, but their code was not wired:
11 wiring tests in `test_group360_structure_refresh.py` and 17 in `test_group358_prepare_structure_notes.py` failed. All of them pass now.

## main.py
- `_start_structure_refresh` / `_start_structure_refresh_locked`: one run at a time (non-blocking lock), `already_running`, `disabled` (STRUCTURE_REFRESH_ENABLED=0),
  `dry_run`, `nothing_to_do`; the work runs on a daemon thread (`structure-refresh`). Never raises into a route or the QStash tick.
- Routes (each with the `/api` alias): `POST /data-feed/structure-refresh?force=&dry_run=&limit=`, `GET .../status`, `POST .../stop`.
  `structure-refresh` is in `_FEED_POST_ONLY_ACTIONS`, so a GET on it is a 405, not the `/data-feed/{symbol}` catch-all.
- Jobs snapshot has a `structure_refresh` entry and `_job_is_active` counts it (the push interval speeds up while it runs).
- QStash tick: `{"action": "structure-refresh"}` (optional `"force": true`). Schedule it once a day after the close (the daily bar is usable ~20 min after 15:30 IST).
- `refresh_prepare_to_buy`: snapshots rows and stored prices BEFORE the quote patch, collects the fresh prices, then adds `structure_mode`, `structure_notes`
  and `structure_counts` to the reply, logs each flagged symbol and bumps `stockky_prepare_structure_total{outcome}`. Log only: every quote is still patched,
  and a failure in the notes path leaves the reply exactly as before. `SNIPER_PREPARE_STRUCTURE_MODE=off` adds nothing.

## buy_sniper.py
`prepare_triggers`, `prepare_refresh_note`, `prepare_refresh_report` (flip = fails at the fresh price, passed at the last stored one). The trigger logic mirrors
decision-prediction `structure_policy.prepare_triggers`; the 4,000-case randomised drift test passes.

## data_feed.py
`DataFeedStore.replace_fields(symbol, fields, drop_keys)`: replace on an EXISTING row only (never creates one), drops the old owned keys first so an old `ms_*`
value cannot survive next to a newer `ms_asof`, writes all three alias keys, False on a missing row, bad input or failed canonical write. (It was missing: five
tests in the group 360 file failed on it.)

## Config
`.env.example` now lists `SNIPER_PREPARE_STRUCTURE_MODE` and the six `STRUCTURE_REFRESH_*` variables; `docs/ENV_REFERENCE.md` regenerated.

## Tests
group 358: 20/20, group 360: 67/67 (stand-in runner, not real pytest). Every other api-gateway test file gave the same result as the uploaded zip.
Not run: tests that need FastAPI / httpx (e.g. `test_main_feed_control_audit.py`, which exercises `refresh_prepare_to_buy` with fakes).

## Update: position-stocks observation hook (same group)
`orders/structure_observation.py` and the `ScalpStructureObservation` model already existed but nothing called them. Now:
- `orders/structure_gate.py` `check(symbol, ltp, capture=None)`: an optional dict receives `verdict`, `summary`, `gate_mode`, `enforced`; empty when the gate is off or failed. Old two-argument calls unchanged.
- `orders/entry.py`: right after the gate, `structure_observation.record(db, ...)` for passed AND rejected verdicts (one row per symbol / IST day, added to the entry's session, never raises, never changes the entry).
- `main.py`: boot loop `position-stocks-structure-obs-loop` (switch `STRUCTURE_OBS_ENABLED`, start isolated in try/except, cancelled on shutdown) and `GET /structure/edge?days=&recent=` (report on a worker thread, read-only, errors returned not raised).
- `config.py`: `STRUCTURE_OBS_ENABLED / _EVAL_INTERVAL_S / _BATCH / _HISTORY_TIMEOUT_S / _GIVE_UP_DAYS` and `STRUCTURE_EDGE_MIN_N` (they were missing, 7 tests of group 356 failed on it). `PS_<name>` override for the shared gate names (group 365 documented it, the code was missing): GATE_MODE, REQUIRE_DATA, BEARISH_BLOCK, MAX_CHASE_ATR, MAX_STRETCH_ATR, MIN_ROOM_ATR.
- Tests: new `test_group372_structure_obs_hook.py` (8, includes a real-SQLite record / restart-dedupe / report round trip); group 356 test updated (a would-reject verdict is always `rejected_enforced` since group 370); group 354 pin test accepts the `_ps(...)` form.
- Real pytest (sqlalchemy, fastapi installed): position-stocks suite went from 12 failed + 14 errors to 3 failed + 14 errors; nothing new fails.

## Update 2: board A/B code (group 362) finally in position-stocks
`docs/GROUP362_BOARD_AB_AND_CALIBRATION.md` described it, `test_group362_board_ab.py` (14 tests) existed, the code did not (14 errors). Now:
- `screening/ms_engine_switch.py`: `compare()` (pure; zones matched on side + idx -> dropped / added / status / bounds / other, swings one-to-one, float noise ignored), `_ab_compare` (legacy maths on the same bars, run BEFORE the newest-6 cap, never raises, never touches what is served; a cap knock-on is counted apart from a contract break), `_record_ab` (counters, 14 days, 50 newest differing samples), `ab_report()` (percentages + verdict `disabled | insufficient_sample (<200) | contract_break (engine_added, swings_differ or errors) | parity_ok`), `reset_ab()`; `zones_and_swings(..., symbol=None)`; `stats()["ab"]` so it shows in `GET /board/status` -> `ms_engine.ab`.
- `screening/board.py` passes `symbol=symbol`. `config.py`: `BOARD_AB_ENABLED` (on), `BOARD_AB_MAX_COMPARES` (100000, 0 = no cap). `.env.example` + `ENV_REFERENCE.md` updated.
- Real pytest: 14/14 plus the group 349 / 351 switch tests. The real verdict still needs a full live session (>= 200 comparisons).

## Update 3: the last three failing position-stocks tests
- `orders/board_gate.py` used `time.time()` with no `import time`: every board-gate log row (`_log`) raised NameError, which the `except` swallowed at debug level, so the entry-time board log was never written in production. Import added (`test_group309 ... test_gate_modes` passes).
- `screening/quality_gate.py` `_event_shared_wait` / `_ft_shared_wait`: when the one in-flight request finished between the gate's first and second wait, its done-callback had already unregistered it, so the second wait started a SECOND request and ignored the kept answer. Both now return the kept late answer (within `QUALITY_GATE_EVENT_LATE_TTL_S`). Regression test added. Pyflakes finds no other undefined name in the service.
- Position-stocks suite: all green (see the reply for the count).

## Not done
- backtest on real data. (Frontend badge labels: done in group 373.)
- `test_group309_board_log_gate_report.py::test_gate_modes` and two `test_group356_event_shared_fetch.py` tests fail identically on the uploaded zip.

## Update: frontend notes list (same group)
`structureView.ts` `prepareNoteLines(notes, max = 8)` (flips first, then turned-bad, then events; alphabetical inside a group; garbage -> []) and `ScanPanel.tsx`:
after "Refresh Prepare-to-Buy" the `structure_notes` of the reply are listed under the status line (amber = structure now blocks entry, green = structure event).
Node test added to `structureView.test.mjs` (passes). `structureView.ts` type-checks alone under `tsc --strict`; full `npm run build` was not run (no node_modules here).
