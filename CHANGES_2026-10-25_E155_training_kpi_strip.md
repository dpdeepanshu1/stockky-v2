# E155 - Training Lab: v6 summary strip (2026-10-25)

Includes E154. Fifth and last of the discover screens in the plan's restyle order. Status: `docs/AUDIT_STATUS_E155.md`.
Roll back: delete `frontend/src/components/trainingView.ts` and `frontend/src/trainingView.test.mjs`, remove the two imports in `Training.tsx`, put back the three `<StatCard>` grid (Last Training, Dataset Size, Symbols) where the one `<KpiStrip .../>` line now sits, and remove `trainingView.test.mjs` from `npm test` in `package.json`.

## What you will see
The three plain stat cards under "Production Model" are now one v6 strip:
| Tile | Shows |
| --- | --- |
| Model | Deployed with its version, or "None deployed" with the warning sign |
| Last training | age (`6.0h ago`) with the IST time; "Never" with the warning sign; an unreadable date shows a dash and says so |
| Dataset | rows stored and the symbol count; a real 0 shows as 0, a dash when not reported |
| Walk-forward win rate | stored win rate with Sharpe and directional accuracy in the sub-line; a dash when no walk-forward metrics are stored |
| Training now | only while a run is going: percent and stage, "Running" when no percent yet, "Error" with the warning sign when progress reports one |
Behaviour change: the old cards showed 0 for dataset size and symbols while status was still missing; the strip is empty until status loads and then shows a dash for anything not reported. No threshold is invented for "too old": the age is shown, not judged. No new request; nothing animates a number. The buttons, progress panel, metric grid, folds, history and rollups are unchanged.

## Files
`frontend/src/components/trainingView.ts` (new, pure), `frontend/src/trainingView.test.mjs` (new, 40th suite), `frontend/src/components/Training.tsx` (+2 imports, three-card grid replaced by one line), `frontend/package.json`.

## Tests
Run here: `npm test` (40 suites), including `trainingView.test.mjs` (no status, nothing trained, deployed model, version fallback, zeros, NaN metric, bad date, running / error tile); strict `tsc` on `trainingView.ts` alone. NOT run here: `tsc --noEmit` on the whole app, `vite build`, a browser, backend pytest.
