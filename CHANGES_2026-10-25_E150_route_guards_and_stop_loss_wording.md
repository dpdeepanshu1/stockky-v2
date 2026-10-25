# E150 - Telegram / delete routes need the login, one "Stop loss" label (2026-10-25)

Includes E149. Complete / remaining list: `docs/AUDIT_STATUS_E150.md`. Matrix and decisions: `docs/ROUTE_AUTH_MATRIX_E149.md`.
Roll back: restore the files named under each heading from the E149 zip; each change is small and separate.

## Behaviour changes you will notice
1. **Seven more gateway routes need the admin login** (F-20): `POST /ipo/notify-top-picks`, `/surprise/ipo/notify-top-picks`, `/surprise/notify-top-picks`, `/stockky-hot/notify-top-picks` (the "Send Top 5 to Telegram" buttons on IPO Tracker, Surprise, Hot Picks), `POST /ipo/purge-non-equity` and `/surprise/ipo/purge-non-equity` (the IPO "Delete Non-Equity Rows" button) and `DELETE /scan/universe/cache` (no caller). Before, anyone who could reach the domain could make the bot message your Telegram or delete IPO rows. **If you press one of these buttons while logged out you now see a message telling you to log in once on Auto Trade, Position Stocks or Calibration; the browser then sends the saved token by itself.** `GATEWAY_ADMIN_GUARD=false` turns the guard off.
2. **One label for the stop price (F-18): "Stop loss"** (target stays "Target"). Changed on: Conviction card, Position Stocks position rows and chart line, Signal stream, Paper Trades, Short Shadow card, Top Picks, Auto Trade chart levels, Manual trade ticket (form and preview). "Stop Loss" with a capital L is now "Stop loss". Job buttons named Stop (stop the data feed, stop Run All) are unchanged.

## Files
- `api-gateway/admin_guard.py` (`PROTECTED` +7), `gateway_support/bound_routes_d.py`, `_f.py`, `_j.py`, `gateway_routers/ipo_routes.py` (imports `require_admin`; the file's `Depends` import added).
- `api-gateway/tests/test_admin_guard.py` - the "PROTECTED equals the guarded decorators" sweep now also reads `gateway_routers/*.py` and matches `require_admin` as well as `_require_admin`.
- `frontend/src/gatewayAuth.ts` + `gatewayAuth.test.mjs` - the seven paths get the saved token; look-alikes (`/ipo/scan`, `/ipo/notify-top-picksx`, `/scan/universe`, `/stockky-hot/run`) tested to stay token-free.
- `frontend/src/api.ts` - a 401 "admin session" answer now ends with the log-in hint.
- Label files: `ConvictionCard.tsx`, `PositionStocksTab.tsx`, `SignalStream.tsx`, `Trades.tsx`, `board/ShortShadowCard.tsx`, `board/TopPicks.tsx`, `realAuto/format.ts`, `trading/ManualTradeTicket.tsx`.
- New `frontend/src/labels.test.mjs` (in `npm test`, now 35 suites): no "Stop Loss", no bare "Stop" label beside a price.
- `docs/ROUTE_AUTH_TRIAGE.json` (7 entries removed), `scripts/test_audit_route_auth.py` (+1 test), `docs/ROUTE_AUTH_MATRIX_E149.md` rewritten with the E150 decisions.

## Tests
Run here: `npm test` (35 suites, exit 0), `scripts/test_audit_route_auth.py`, the `PROTECTED` vs decorator sweep re-computed by hand (24 = 24), `audit_route_auth.py --check`, compile of `api-gateway` and `scripts`, shared-copies and secrets checks.
NOT run here (no `fastapi`, `pytest`, `tsc`, `esbuild`, browser): `services/api-gateway/tests/test_admin_guard.py`, `tsc --noEmit`, `vite build`. Run them on the VM.
