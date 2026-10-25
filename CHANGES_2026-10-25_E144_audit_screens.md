# E144 - the audit's "no screen" gaps closed: Swing lane, Reconciliation, Status probes (2026-10-25)

Follow-up of E142 (tab audit) and E143 (live field audit). Frontend only: no trading code, no switch, no endpoint changed. Every new screen is READ-ONLY.
Where: **Real Money Calibration** page, three new sections next to "Short selling" (same fetch / poll / card pattern as `ShortSellingPanel`).

## What was added
| Section | Shows | Calls (all GET) | Poll |
| --- | --- | --- | --- |
| **Swing lane** | mode and what it may do, the shadow verdict (NOT_ENOUGH_DATA / USEFUL / NO_EDGE) with progress to 30 trades / 20 days, net / gross mean, win rate, stopped %, 90% interval, newest scan + stock list, wiring problems, paused modes, open swing positions, tomorrow's planned picks, newest 40 shadow ledger rows | `/swing/status`, `/swing/ledger?kind=SHADOW&limit=40` (real-trade) | 30 s, only while the browser tab is visible |
| **Reconciliation** (Real Trade tab) | the hourly broker-vs-book drift report: warnings (missing at broker, book over broker, stale order) and notes | `/reconcile-report/REAL` (the stored report, never `run=true`) | 60 s, visible tab |
| **Reconciliation** (Position Stocks tab) | exits still waiting for their real fill price, age, how each resolves | `/reconcile/pending` | 30 s, visible tab |
| **Status probes** | 9 real-trade + 3 position-stocks status routes that had no screen (`/adaptive/status`, `/preopen/status`, `/review-fixes/status`, `/range52w/status`, `/regime-rules/status`, `/kronos/status`, `/timesfm/status`, `/write-behind/status`, `/ops/db-latency`; `/short/plain`, `/short/probe`, `/board/status`) | one GET per button press | none (on demand: some read the database) |

Also: the **EOD square-off** switch text on Auto Trade -> Pipeline now says the swing lane needs it ON (the wiring check already reported this; the page did not).

## Files
New: `swingView.ts`, `reconcileView.ts`, `statusProbes.ts` (pure, no imports); `swing.test.mjs`, `reconcile.test.mjs`, `statusProbes.test.mjs`; `components/calibration/{SwingPanel,ReconcilePanel,StatusProbesPanel}.tsx`.
Changed: `realTradeApi.ts` (types `SwingStatusDoc`, `SwingReportDoc`, `SwingLedgerDoc`, `ReconcileReportDoc` ... and calls `swingStatus`, `swingLedger`, `reconcileReport`, `probe`), `positionStocksApi.ts` (`PendingReconcileDoc`, `reconcilePending`, `probe`), `CalibrationControlTab.tsx` (3 sections), `PipelineTab.tsx` (one sentence), `calibration.test.mjs` (the "Run all" pin now also lists the three new sections, which start no test), `package.json` (`npm test` runs the three new suites).
The types sit in `realTradeApi.ts` / `positionStocksApi.ts` on purpose: `scripts/audit_live_fields.py` now checks `/swing/status`, `/swing/ledger`, `/reconcile-report/REAL` and `/reconcile/pending` against the live JSON automatically (required fields only; the probes are `any`, not checked).

## Choices worth knowing
- The "top X%" label is NOT shown on swing picks: what `pct_rank` means was not confirmed, so the card shows the rank number only (a test fails if the label comes back).
- No write is offered anywhere: swing scan / resume / probe, holdings-sync, "Resolve stuck now" and the SWING_ switches stay on their existing places or the API. Tests pin that the new panels call only the read routes.
- eDIS needed no new screen: Auto Trade's overview already polls `/dhan/edis/summary` (the morning TPIN state). `/dhan/edis/status` is the raw per-ISIN inquiry behind it.
- The three new sections hide the "Run all" bar (they start no test), like Market Weather.

## Checked here
- `npm test`: all 23 suites pass (3 new: swing, reconcile, statusProbes). Mutation check: flipping the NO_EDGE tone in `swingView.ts` fails `swing.test.mjs`.
- `tsc --noEmit` clean; real `npm run build` (tsc + vite) succeeds (`npm ci` worked in this sandbox).
- Contract tests read the Python source: every key of `SwingStatusDoc`, `ReconcileReportDoc`, `PendingReconcileRow` is written by the backend; every status-probe path is a GET route.
- The real `swing.engine.report()` and `MemRepo` ledger output (empty ledger and 25 closed rows) carries every required field of `SwingReportDoc`, `SwingStats`, `SwingLedgerRow`.
- `scripts/test_audit_live_fields.py` (6) passes; the checker's parser finds the four new typed calls.

## NOT checked / still open
- Not clicked through in a browser; not run against your live services (needs the VM). Run `TOKEN=... python3 scripts/audit_live_fields.py` on the VM: it now also covers the new calls. Then open Calibration -> Swing lane, Reconciliation (both service tabs) and Status probes once.
- `/swing/status` runs the shadow report (a 500-sample bootstrap once 20 entry days exist) on every poll: cheap, but if the page feels slow, lengthen the 30 s poll in `SwingPanel.tsx`.
- Still API-only on purpose (parameterised look-ups or writes): real-trade `/account/reset-starting-capital`, `/reconcile/{mode}/holdings-sync`, `/swing/scan|resume|probe`, `/dhan/edis/status`, `/board-overlay/{}`, `/structure/observations/{}`; position-stocks `/reconcile/pending/resolve`, `/symbol-lock/{}`, `/orders/events/{}`, `/trades/cleanup`, `/trades/breakdown`, `/board/intraday/{}`, `/board/self-check` (slow, not for polling). The tab audit's "not referenced" count still lists routes the control page builds from a prefix (`/control/*`, `/approvals/*`): those are false positives (E142).
- The swing lane itself is unchanged: the replays say NO_EDGE (E138/E140), the shadow verdict needs weeks of data.
