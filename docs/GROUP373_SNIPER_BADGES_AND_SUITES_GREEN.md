# Group 373 - Buy Sniper structure badges, and every service suite green

## Frontend (item "badge labels")
- `structureView.ts`: `sniperBadge(structure)` (Structure OK / No structure read / Structure warns; null when the record is missing or the gate is off, so there is never a false "OK") and `sniperHeldBackLines(rejected, max)`.
- `BuySniperModal.tsx`: a badge next to the action badge on every card (tooltip = chase / R:R or the no-data reason), a "Held back by the daily-structure check" list, and the gateway message when every setup was held back. `ScanPanel.tsx` passes `structure_rejected` and `message`; `api.ts` types them.
- Node test `structureView.test.mjs` extended (passes). `tsc --noEmit` clean, `npm run build` ok. Not browser-tested.

## Test suites (run in the sandbox, Python 3.12, pinned requirements)
| service | result |
|---|---|
| analysis-intelligence | 2532 passed |
| notification-scheduler | 188 passed |
| market-data | 1704 passed |
| real-trade | 4628 passed, 1 skipped |
| api-gateway | 8821 passed |
| decision-prediction (decision / prediction / training) | 113 / 62 / 61 passed |
| position-stocks | 3756 passed (group 372 run; not touched here, rerun on the VM) |

## Code fixes found by those runs
- `real-trade-service/exit_engine/exit.py`: the structure-exit step had a second silent `except: pass` (after a rollback); it now logs at debug. The pinned count stays at 1.
- `analysis-intelligence-service/news/feed_fetch.py`: `_warn_throttled` warns for a key it has never seen whatever the clock reads.
- `decision-prediction-service/training/app.py`: the group 359 entry-structure wiring was missing. The pick model accepts `structure`; `store_prediction` and the commit path store it once inside `feature_snapshot` (`structure_entry.attach`), a re-click backfills it once before the trade opens; `GET /api/trades` returns `structure` per trade (a failed lookup returns none, never an error).

## Stale / fragile tests fixed (no production change)
- analysis: HF "not supported" pause is 21600 s since group 341.
- market-data: batcher test releases all callers at once and widens the batch window (it split under CPU load).
- real-trade: groups 351, 355, 356 set the structure gate to enforce (group 370 made it always live; the shared fixture leaves it off); group 360 live-tick test supplies `prev_close` (group 230 rule for Tier 3).
- api-gateway: fake `Engine.scan` accepts `symbols` / `force_reload_static` (the boot warm now goes through `_start_surprise_scan`); job-snapshot tests expect the group 372 `structure_refresh` entry.

## Still open
Backtest on real data; board / structure A/B verdicts need real live sessions; the earlier real-trade, position-stocks and decision `main.py` edits still unrun on your VM; `npm run build` ran here, the VM deploy did not.
