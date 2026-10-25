# E147 - audit closed; `check_control_catalog.py` no longer re-parses every file per switch (2026-10-25)

Script only (CI / pre-deploy check). No service code, frontend, API or switch changed. Includes E146.

## Audit result (all run in a sandbox on Python 3.12; your VM runs 3.14)
| Suite | Result |
| --- | --- |
| real-trade-service | 5373 passed, 1 skipped |
| position-stocks-service | 4315 passed |
| api-gateway | 9044 passed |
| market-data-service | 1733 passed |
| analysis-intelligence-service | 2643 passed |
| calibration-runner-service | 258 passed |
| notification-scheduler-service | 214 passed |
| decision-prediction-service (training/tests, prediction/tests, decision/tests) | 152 + 62 + 173 passed |
| timesfm-forecast-service | 32 passed |
| kronos-forecast-service (no torch installed; the tests do not need it) | 11 passed |
| scripts/ | 523 passed, 1 skipped (one test timed out at my 60 s limit on the slow check below; re-run alone: 20 passed) |
| frontend | `tsc` clean, 25 test suites pass, `vite build` succeeds |
Static: every service compiles, no merge-conflict markers, no undefined names (pyflakes), compose/ports/requirements-drift/secrets/shared-copies/table-name/ENV_REFERENCE/control-catalog checks OK, tab audit 0 real unrouted paths.
Browser: the E146 rail (normal long, trailed long, trailed short, price at the moved-up stop) was rendered in headless Chromium with the real built CSS, dark and light, no console errors; the red stop tick sits right of the entry tick and the marker colours are right. That used a static page with the component's markup, not the live pages with live data.
Not run: anything needing live credentials (`check_reconcile_fixes.py`), a click-through of the real pages with live data.

## Fix
`scripts/check_control_catalog.py` parsed every service file again for every switch target (switches x files): more than 5 minutes for the two catalogs here. Trees and import-alias lookups are now cached for the run. Same verdict ("checked 2 catalogs: ok"), about 58 s.
