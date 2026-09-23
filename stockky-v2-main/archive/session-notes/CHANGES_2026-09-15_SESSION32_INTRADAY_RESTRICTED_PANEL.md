# Session 32 — Finish Intraday-Restricted Symbols Panel + Fresh Audit Pass

**Requested:** "audit position stock tab fully and real automate trade and
give me updated zip in same format as original"

## Context
The uploaded zip already reflected an in-progress session: backend wiring
for a learned "intraday-restricted symbol" list
(`ScalpIntradayRestrictedSecurity` / `screening/intraday_eligibility.py`)
was complete — symbols get recorded when `orders/eod_squareoff.py` or
`orders/entry.py` see a live Dhan SELL rejection classified by
`execution/dhan_client.is_security_intraday_restricted_error()`, and the
list is already consulted to filter candidates in `main.py`'s cycle —
but there was no API route and no frontend panel surfacing it, so the
learned list was invisible on the dashboard. This session finished that.

## Completed
- **Backend:** added `GET /candidates/restricted` to
  `services/position-stocks-service/main.py` — pure read of
  `ScalpIntradayRestrictedSecurity`, same shape/pattern as the existing
  `GET /candidates/log`. Updated the route-list docstring at the top of
  the file to match.
- **Frontend types/api:** added `ScalpIntradayRestrictedRow` type and
  `candidatesRestricted()` method to `positionStocksApi.ts`.
- **Frontend UI:** added an "Intraday-Restricted Symbols" panel to the
  Screener sub-tab of `PositionStocksTab.tsx`, directly below the existing
  Candidate Log table — symbol, hit count, first/last seen, last rejection
  detail. Wired a `loadRestrictedSymbols()` loader into the same
  mount-effect and 30s poll cadence as the other Screener-tab data.

## Fresh audit pass this session (no new bugs found)
- Re-read `cycle_runner.py` (real-trade-service) end to end — all `pass`
  statements are legitimate best-effort try/except suppression, no
  swallowed-gate-check pattern like sessions 4/12's `continue`-vs-`pass`
  bugs.
- Cross-checked every `realTradeApi.ts` method against every
  `services/real-trade-service/main.py` route — all present on both
  sides. Five backend routes have no frontend caller
  (`/positions/{mode}/history`, `/adaptive/status`,
  `/adaptive/market-params/status`, `/reconcile/{mode}/holdings-sync`,
  `/dhan/edis/status`) — read each one; all are intentional
  diagnostics/manual-ops endpoints (documented as such in their own
  docstrings), not a wiring gap like session 22b's order-id finding, so
  left as-is.
- Re-read `execution/dhan_client.py` (position-stocks-service) in full —
  tick-rounding, security-cache, and the three rejection classifiers are
  all correct and already carry this session's own earlier tick-rounding
  fix (documented inline).

## Verification
- `python3 -m py_compile` clean on every `.py` file in
  `position-stocks-service` (main.py, models.py, orders/*, screening/*,
  execution/*, capital/*, auth/*, feed/*, resilience/*, db.py, config.py,
  tz_utils.py, oracle_compat.py).
- `npx tsc -p tsconfig.json` (real tsconfig, not an isolated stub check):
  **zero errors** except the expected `vite/client` type-definition
  warning caused by `node_modules` not being installed in this sandbox
  (no network egress here) — nothing in `PositionStocksTab.tsx`,
  `positionStocksApi.ts`, `realTradeApi.ts`, or `RealAutoTrade.tsx`.
  Real `npm install` still recommended on the actual dev machine/VM
  before deploy, same standing caveat as every prior frontend session.

## Not changed
Everything else in both services — left as audited clean by sessions
19–31 (see those session-notes files for what each already covered in
depth: screening/, capital/, feed/, resilience/, auth/ on position-stocks;
watchlist_engine/, risk_engine/, portfolio/, manual_engine.py on
real-trade-service).
