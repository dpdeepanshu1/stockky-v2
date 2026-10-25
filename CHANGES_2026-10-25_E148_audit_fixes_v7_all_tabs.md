# E148 - audit round 2: training-proxy login gap, "0 treated as missing" fixes, v7 header on 12 of 13 tabs (2026-10-25)

Includes E147. Full complete / remaining list: `docs/AUDIT_STATUS_E148.md`.
Roll back: restore the files named under each heading from the E147 zip (every change is small and separate).

## Behaviour changes you will notice
1. **Training Lab: Train, Clear lock and Promote model now need the admin login** (same login as Auto Trade). Before, only `POST /training/train` was guarded; the same actions were reachable through the gateway's generic `/training/{path}` proxy with no login. Reading the Training Lab and its progress poll still need no login. If the Training Lab shows "Missing admin session token" on those buttons, log in once on Auto Trade, Position Stocks or Calibration and press the button again.
2. **A score of exactly 0 is no longer replaced by a default** in three places (see F-11). This can change a score only when the stored value really is 0.
3. **Every tab except the Dashboard now opens with the shared v6/v7 hero** (icon, kicker, title, one-line description, "Animations: On/Off" switch). Cards below the hero are unchanged (still the v3-v5 look); restyling them is the next step.

## Fixes
| ID | Sev | What | Files | Test |
| --- | --- | --- | --- | --- |
| F-12 | S2 | `/training/*` proxy forwarded train / train-run / models-promote / lock-clear / admin / debug-abort with no login. Now `admin_guard.training_path_needs_admin(method, path)` decides (case, `//`, `/./`, trailing `/` and `..` cannot hide a path; reads and the routes the UI uses without login stay open) and the proxy calls the admin guard before forwarding. The browser helper attaches the saved token for exactly those paths. | `api-gateway/admin_guard.py`, `gateway_support/bound_routes_h.py`, `frontend/src/gatewayAuth.ts` | `tests/test_e148_training_proxy_guard.py`, `gatewayAuth.test.mjs` |
| F-11 | S2 | `float(x or 100.0)` style defaults treated a real 0 as missing: surprise scanner 52-week distance 0.0 (stock AT its high lost the "Near 52W High" points), instant scanner sentiment 0, decision technical score 0. Only `None` now falls back. | `api-gateway/surprise_scanner.py`, `api-gateway/instant_scanner.py`, `decision-prediction-service/decision/main.py` | `tests/test_e148_falsy_zero.py`, `decision/tests/test_e148_tech_score_zero.py` |
| F-13 | S2 | `normalize_feed_payload` passed NaN / inf from an upload straight into the merge (NaN never equals itself; `json.dumps` writes the invalid token `NaN`). They now become "missing" and `strip_none_fields` drops them, so the stored metric is kept. | `api-gateway/data_feed.py` (+ `import math`) | `tests/test_e148_feed_nonfinite.py` |
| F-05 | S3 | v7 hero on 12 of 13 tabs through `PageHeader(icon, kicker)`; a hidden `h1` keeps one page heading for screen readers. | `ui/PageHeader.tsx`, `app/TabContent.tsx`, `app/SettingsPage.tsx` | `design-all-tabs.test.mjs` |
| F-09 | S3 | The saved "Animations: Off" choice is applied at start-up, so it holds on the Dashboard too (which has no switch). | `main.tsx` | `design-all-tabs.test.mjs` |
| F-14 | S3 | `board/topPicks.ts` and `board/TopPicks.tsx` differed only by case: they collide on a case-insensitive disk (Windows, default macOS). Helper renamed `topPicksView.ts`. | `components/board/` | `topPicks.test.mjs` |
| F-16 | S3 | `stopout_guard.pause_reason_from` / `symbol_reason` stay fail-open (an error never blocks an entry) but now log one warning per kind per process saying the guard is OFF and which settings to check. No rule change. | `real-trade-service/entry_engine/stopout_guard.py` | `tests/test_e148_silent_guards_warn.py` |
| F-17 | S4 | A malformed `REGIME_RULES_TABLE_JSON` still falls back to the default table, with one warning per distinct bad value. | `real-trade-service/entry_engine/regime_rules.py` | same file |
| F-15 | S3 | 8 existing test files were not in `npm test` (alignment + 7 board suites). Now listed; `npm test` runs 34 suites. | `frontend/package.json` | - |

## New read-only scripts (nothing here changes a file)
- `scripts/audit_functions.py` - function baseline per service; `--falsy` lists the zero-as-missing candidates (38; the only non-zero default left is `swing/live.py:255` `stop_pct or 3.0`, which is safe: a 0% stop means no stop, so 3% is the right fallback); `--silent` lists `except Exception: pass` (608; 45 in order / capital / exit / reconcile paths).
- `scripts/audit_ui_coverage.py` - which header and which v6/v7 pieces each tab uses, and how many v3-v5 card classes remain.
- `scripts/audit_tabs.py` - now also covers IPO Tracker, Hot Picks, Data Feed, Surprise Momentum and Training Lab (every call they make has a backend route), and understands `api_route(...)` and `APIRouter(prefix=...)`, so the training universe routes and `/notifications/call/me` are no longer false "NO ROUTE" hits.

## Review result (no code change)
- 45 silent `except Exception: pass` handlers in order / capital / exit / reconcile code: 25 are `db.rollback()` guards inside an error path, 4 are alert sends ("the alert must never break the gate"), the rest are status or record-only calls. **No S1.** Two were fail-open guards without a log line (F-16, now logged).
- F-07 (Real Trade rail uses `side="LONG"`): safe today, `TradePosition` has no side column; revisit when shorts are added there.

## Open findings found here (not changed)
| ID | Sev | Finding |
| --- | --- | --- |
| F-18 | S3 | Stop wording differs across screens: "Stop loss" x4, "Stop" x9, "Target" x8 (no "SL" / "TP"). Pick one label. |

## Checked here
34 frontend suites pass (`node --experimental-strip-types`); esbuild bundles `main.tsx` with no error; every service, `shared/` and `scripts/` compile; secrets, merge-conflict, requirements-drift, compose, table-name, shared-copies, market-structure package, holiday, price-source and market-data-URL checks pass; the new Python tests were run with a small stand-in runner (training-guard cases run against `admin_guard` with stub imports).

## NOT checked (needs your VM)
`pytest` for any backend service (no package registry in the sandbox), `tsc --noEmit`, `vite build`, a browser look at the new hero on the 8 re-headed tabs, and any live-credential check. Commands: `./stockky_full_test.sh`, `cd frontend && npm ci && npx tsc --noEmit && npm test && npm run build`.
