# Group 355 - structure gate status (real-trade + position-stocks), backend and frontend

The gates were already live in group 354 (real-trade `STRUCTURE_GATE_MODE=enforce`, position-stocks `orders/structure_gate.py` enforce, structural stop on). What was missing was any way to SEE what they do. This group adds read-only observability; nothing in the entry path reads it back.

## real-trade-service
- `entry_engine/structure_gate.py`: process counters (`evaluated / ok / no_opinion / would_reject / enforced / nodata_blocked`, reason and no-data buckets, fetch cache / ok / fail / disabled), a 100-entry ring of recent verdicts, `stats()`, and the pure `tally_decisions()` over stored ENTRY decisions (coverage, enforced vs shadow-only, reason buckets, rejected symbols with the move since). `reset()` also clears the counters.
- `entry_engine/entry.py`: one `structure_gate.record(...)` call right after the snapshot is stored (inside the existing fail-open try).
- `main.py`: `GET /structure/status/{mode}?hours=24` (DEMO open, REAL admin like every other `/{mode}` route). Returns `{mode, gate_mode, hours, process, stored}`; live prices only for the rejected symbols, display only.
- Tests: `tests/test_group355_structure_status.py` (pure, 31 incl. the real `evaluate()` reason wording), `tests/test_group355_structure_status_wiring.py` (entry feeds the counters; needs service deps).

## position-stocks-service
- `orders/structure_gate.py`: same counters / ring, `record()` called from `check()`, `with_moves()` (adds now / since_pct to the ring from `ws_client.get_last_ltp`), `tally_stored()` (refusals from `scalp_candidate_log` reasons `STRUCTURE_BAD_LOCATION` / `STRUCTURE_NODATA`, and the snapshots stored on `scalp_positions`).
- `main.py`: `GET /structure/status?hours=24` (open, like `/board/status`).
- No schema change: a refused scalp stores no price, so the "move since" for position-stocks comes from the in-memory ring (since restart).
- Tests: `tests/test_group355_structure_status.py` (28, pure).

## Frontend
- `structureView.ts`: `statusView(kind, raw)` normalises both replies into one view model (checked / blocked / shadow / no read / coverage, reason and no-data counts, turned-away list with since %, outcome line); `modeInfo`, `sinceText`, `sinceTone`, `statusHeadline`, `outcomeLine`. Node tests in `structureView.test.mjs`.
- `components/StructureGateCard.tsx`: collapsed by default, 24h / 7d, refresh, 60 s poll only while the tab is visible, plain-words labels, shadow always labelled "not applied".
- Mounted in `RealAutoTrade.tsx` (Candidate watchlist tab, above the list, per mode; REAL without admin login shows a login hint instead of a failing request) and `PositionStocksTab.tsx` (above the Candidate Log).
- `realTradeApi.structureStatus(mode, hours)`, `positionStocksApi.structureStatus(hours)`.

## Config / deploy
No new env vars, no compose or nginx change (`/realtrade/` and `/positionstocks/` prefixes already proxy new routes).

## Verified / not verified
- Run and passing: real-trade pure gate tests (48 = 31 new + 17 group351), position-stocks pure gate tests (28 new + the group354 gate tests, all but one; the one other failure needs sqlalchemy, absent here), `node --experimental-strip-types structureView.test.mjs`.
- NOT run: the FastAPI routes, the DB-backed wiring test, the full pytest suites of both services, `tsc` with real React types and `vite build`. The build sandbox could not reach pypi / npm (egress policy 403). A stubbed-React `tsc` pass showed no errors in the new or edited code. Run both suites and `npm run build` before deploying.
