# Stockky — Session 2026-09-03: Regenerate-token endpoint (finally merged) + Vercel build fix

## What was actually wrong

SyncContext entries #65/#67/#68 said the Dhan "Regenerate token" backend
endpoint + button had been written and merged. Checked the zip you just
uploaded (commit b26fb1e) directly: it was NOT there. `grep -rn regenerate`
across the whole repo returned nothing except a comment. So the button was
never actually missing from your eyes — it was never actually in the tree
you deployed. Confirmed by reading the files, not by trusting the log.

The Vercel build log you pasted is a real, separate bug: `StockChart.tsx`
passed `onTouchStart/onTouchMove/onTouchEnd` straight to recharts'
`<AreaChart>`, and recharts' TS types don't declare those props (works at
runtime, fails `tsc`). Every deploy since that file was added has been
failing at the type-check step, so Vercel has been silently serving the
last **successful** build (that's what "Restored build cache from previous
deployment" means) — which is also why your live site doesn't reflect
recent frontend changes even when they compile fine locally.

## What's in this zip (4 files, apply over your current tree)

- `services/real-trade-service/main.py` — added `POST /dhan/regenerate-token`.
  Calls the same `dhan_credentials.refresh_if_totp_enabled()` your
  background TOTP loop already uses. Returns 409 with a clear message if
  `DHAN_TOTP_ENABLED=false` (your current default — manual paste mode)
  instead of silently doing nothing.
- `frontend/src/realTradeApi.ts` — added `dhanRegenerateToken()`.
- `frontend/src/components/RealAutoTrade.tsx` — added a "⟳ Regenerate
  token" button next to the existing "Rotate token" button, in the Dhan
  account card (Overview tab). Own loading state, errors go to the shared
  error banner.
- `frontend/src/components/StockChart.tsx` — moved the touch handlers into
  an `any`-cast spread instead of passing them as typed props, so `tsc`
  stops rejecting them. Scrub-while-dragging behavior on mobile is
  unchanged (same handler functions, same recharts runtime path).

## Verified, not assumed

- `python3 -m py_compile main.py` — clean.
- `npm install && npm run build` run for real in a sandbox (not just
  syntax-checked) — **build now completes with exit code 0**, zero
  TypeScript errors. This is the actual command Vercel runs.

## Pipeline / frontend-mapping check you asked for

Checked whether the Round-1/3 spec work (watchlist_engine, resilience/
circuit_breaker.py, resilience/local_cache.py, tiered source ladder) has
any frontend surface:
- `trade_watchlist` table + `watchlist_engine/` — **is** surfaced: your
  "Watchlist (31)" tab reads from it.
- `resilience/circuit_breaker.py` and the tiered-source status — **no**
  backend endpoint exposes breaker state or which source tier a candidate
  came from, and nothing in the frontend calls for it. That's a real gap,
  not a display bug — there's no data to show yet. Net-new work if you
  want it on the dashboard (which source tier fired, whether a breaker is
  open) — say the word and I'll scope it as its own round.
- The "Pipeline" *tab* you see in the Real Auto Trade screenshots (Candidate
  → Risk Check → Place Order → Fill → Position Open → Exit, Auto-Pilot,
  Scheduled Automation) is real and wired to `GET /pipeline/status/{mode}`
  — that part of what you're seeing is accurate live state, not decoration.
  The unrelated `frontend/src/components/Pipeline.tsx` file (8 hardcoded
  fake stages) is a *different*, unused decorative component that only
  the main scan dashboard in `App.tsx` renders — it's dead weight, not
  connected to anything you asked about, left alone this round.

## Still open / not done this round

- Docker image rebuild on your Oracle VM for the sklearn pin + db.py fix
  from the prior session (entry #68) — not a code issue, just needs
  `docker compose build --no-cache` + restart.
- Circuit-breaker / source-tier visibility on the dashboard (see above) —
  deferred pending your go-ahead.
- Groww-style dashboard reskin — still explicitly deferred per entry #65,
  no design decisions made.
