# E142 - code audited against the four tabs (2026-10-25)

Tabs (frontend/src/components/app/TabContent.tsx): **Auto Trade** (RealAutoTrade, real-trade-service), **Position Stocks** (PositionStocksTab, position-stocks-service), **Real Money Calibration** (CalibrationControlTab; runner + the /control switches of both services + the gateway's /scheduler-control), **Paper Trades** (Trades via the gateway to decision-prediction `/training/api/trades`).
Method: `scripts/audit_tabs.py` (new, read-only, stdlib) lists the API paths each tab's code uses and the routes each service defines, both directions; plus the frontend's own `npm test` suite run as `node --experimental-strip-types`.

## Frontend -> backend (does every call have a route?)
| Tab | files | API paths used | no matching route |
| --- | --- | --- | --- |
| Auto Trade | 19 | 50 | 0 |
| Position Stocks | 26 | 52 | 0 |
| Real Money Calibration | 16 | 23 | 0 real (6 prefix-built paths: `/control` + `/flags`, `/preset/recommended`, `/audit`, `/calibration` nginx base, `/scheduler-control` gateway proxy - all confirmed by hand) |
| Paper Trades | 3 | 107 | 0 real (3 flagged: `/notifications/call/me` gateway, `/training/api/universe/ingest` and `/train-from-universe` under the `/api/universe` router prefix - confirmed) |
No broken link between any tab and its backend.

## Backend -> frontend (features with no screen)
- **Swing lane: all 8 routes** (`/swing/status, /rank, /scan, /ledger, /report, /snapshots, /resume, /probe`) have no UI. The swing switches (`SWING_*`) and jobs do show on the Real Money Calibration page (control catalog and runner catalog are dynamic); the status, wiring block, shadow verdict and open positions are API-only (`curl`). Not built: the replays say NO_EDGE (E138/E140), so a status page is optional.
- Reconciliation has no screen: real-trade `/reconcile-report/{mode}`, `/reconcile/{mode}/holdings-sync`; position-stocks `/reconcile/pending/resolve`.
- Also API-only: real-trade `/dhan/edis/status` (the morning TPIN state; only the authorize form is wired), `/account/reset-starting-capital`, `/board-overlay`, `/structure/observations`, `/ops/db-latency`, status probes (`/adaptive/status`, `/kronos/status`, `/timesfm/status`, `/preopen/status`, `/range52w/status`, `/regime-rules/status`, `/review-fixes/status`, `/write-behind/status`); position-stocks `/short/plain`, `/short/probe`, `/symbol-lock/{symbol}`, `/orders/events/{id}`, `/trades/cleanup`, `/trades/breakdown`, `/board/intraday`, `/board/self-check`.
- The EOD square-off switch the swing entry gate needs lives on **Auto Trade -> Pipeline -> Scheduled Automation** (its text does not mention the swing lane).

## Defects found and fixed (frontend only, no trading code)
1. **`npm test` was red**: `calibration.test.mjs` expected 7 resumable jobs, the runner has 8 (`swing_backtest` is resumable since E131). Test updated. 19 of 20 suites were already green; all 20 pass now.
2. **Swing jobs showed on the Position Stocks section** of the Calibration page (they were missing from `JOB_SERVICES`, so "anything not listed shows on both"). `swing_backtest`, `swing_shadow_report`, `swing_universe` now map to real-trade only; 3 assertions added (mutation-checked: removing the mapping fails the test).
3. **The written run plan had no swing row.** Added: Friday (swing shadow report, also in the Friday pack) and "on demand" (swing_universe + swing_backtest, with the 25 Oct NO_EDGE result). The plan test confirms the job ids exist.

## Checked here / not checked
All 20 frontend `.test.mjs` suites pass; `audit_tabs.py` runs. NOT done: `tsc`/`vite build` (no node_modules, no network), a field-by-field check that each tab's TypeScript types match the backend JSON, and clicking through the UI.
