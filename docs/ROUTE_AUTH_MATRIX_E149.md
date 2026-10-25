# api-gateway route-auth matrix (E149, updated in E150)

Source: `python3 scripts/audit_route_auth.py` (read-only). 109 mutating routes (POST / PUT / PATCH / DELETE, distinct path + verb; `/api/...` aliases counted separately). **27 need the admin login, 82 do not.** Triage per open route: `docs/ROUTE_AUTH_TRIAGE.json`; `--check` fails on a new open route that is not triaged.

| Class | Routes | Meaning |
| --- | --- | --- |
| ADMIN (guarded) | 27 | 17 before E149; +3 in E149 (`/ops/qstash/publish`, both `purge-over-cap`); +7 in E150 (4 Telegram senders, IPO purge x2, `DELETE /scan/universe/cache`) |
| ui-open | 55 | A screen calls it with no login (feed run / stop / repair, scans, IPO, Hot Picks, Surprise, watchlist, price alerts, wake / keepalive) |
| machine | 9 | Called by a cron, workflow or another service with no token (`/catalysts/alert`, `/ops/check-alert`, structure-refresh, wake) |
| machine-signed | 1 | `/ops/qstash/tick` - verifies the Upstash signature itself, fails closed |
| uncalled | 17 | No caller in the repo: alias twins of a called route, plus `/ops/refresh-static-params` |

## What the 82 open routes can do
| Risk | ui-open | machine | uncalled |
| --- | --- | --- | --- |
| Starts a background job (load on services and data providers) | 31 | 6 | 14 |
| Stops a running job | 10 | 0 | 3 |
| Sends a Telegram message (`/catalysts/alert` only) | 0 | 1 | 0 |
| Deletes data (watchlist row, price alert) | 3 | 0 | 0 |
| Writes user data (watchlist, alerts, IPO row) | 6 | 0 | 0 |
| Heartbeat / wake-up | 5 | 2 (+1 signed) | 0 |

None of them places or cancels a broker order or changes a trading setting (those live in real-trade / position-stocks, which have their own login). Worst realistic abuse of an open route: burning provider rate limits, stopping a feed job, deleting a watchlist row or a price alert.

## Decisions taken in E150 (yours to reverse)
1. **Guarded:** every route that can message you or delete shared data: the four `notify-top-picks` routes (IPO, Surprise IPO, Surprise, Hot Picks), `/ipo/purge-non-equity` and its `/surprise/ipo/` twin, `DELETE /scan/universe/cache`. The four Telegram buttons and the IPO "Delete Non-Equity Rows" button now need you to be logged in once (Auto Trade, Position Stocks or Calibration); logged out they show "401: Missing admin session token - log in once on ..., then press the button again."
2. **Left open on purpose:**
   - The 31 + 10 job start / stop routes the screens use. Guarding them would lock a logged-out visitor out of the data feed, scans and Hot Picks, and the worst result of abuse is provider load.
   - The 17 uncalled routes: they are twins of routes that stay open, so guarding only the twin protects nothing. `/ops/refresh-static-params` stays open because it may be called by an outside nightly schedule.
   - Watchlist and price-alert add / delete: other services call `/watchlist` with no token.
   - `/catalysts/alert`, `/ops/check-alert`, `/wake-all`, keepalive: a GitHub workflow and `scripts/wake_all.sh` call them with no token. A separate service key is the clean fix; it needs a secret on the gateway and in the workflow, so it is not done here.
3. If you later want job start / stop behind the login, it is one change per group: add the path to `PROTECTED` and to `gatewayAuth.ts`, then run `audit_route_auth.py --check`.

## Heuristic limits
Callers are found by searching the path text, so a path built from pieces in code is reported as "no caller". "uncalled" means not found in the repo; an external cron (VM crontab, Upstash schedule, GitHub secret URL) is invisible to it.
