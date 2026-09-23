# Changes — 2026-09-02 session (Dhan manual regen + notification detail + harness fix)

Every path below is relative to your repo root (`~/stockky-v2/` on the VM).
These are **whole-file drop-ins** — each file here is the complete file with
the change applied, not a diff/patch. Back up or `git diff` before
overwriting, since I could not run these against your live containers
(no network in the sandbox this was written in) — reviewed and
syntax-checked only, not executed.

## 1. services/real-trade-service/offline_test_harness.py
Your uploaded v2 harness, with one bug fixed: `technical_flags()`'s 21-day
"extended" return was `closes[-1] / closes[-22]` — off by one day vs.
production (`analysis-intelligence-service/technical/main.py` uses
`close.iloc[-1] / close.iloc[-21]`). Now uses `closes[-21]` to match exactly.
Nothing else changed from what you uploaded.

## 2. services/real-trade-service/main.py
New endpoint: `POST /dhan/regenerate-token`.
- Calls `dhan_credentials.refresh_if_totp_enabled(db)` — the same function
  your background `_totp_refresh_loop` already calls on a schedule — but
  on-demand.
- Returns **409** with a clear message if `DHAN_TOTP_ENABLED=false` (your
  current default) instead of silently doing nothing, so the button always
  tells the frontend which path actually ran.
- Returns **502** if Dhan/TOTP call itself fails (bad secret, wrong PIN,
  Dhan unreachable) — check server logs for the underlying error.
- On success, logs `DHAN_TOKEN_MANUAL_REGENERATE` to the audit log and
  returns the same shape as `GET /dhan/status`.
- Requires `DHAN_TOTP_SECRET`, `DHAN_CLIENT_ID`, `DHAN_PIN` env vars to
  actually succeed — same requirements the background loop already has.
  **If you haven't set these, the button will 409 every time** — that's
  expected, not a bug; use "Rotate token" (manual paste) instead in that case.

## 3. services/real-trade-service/exit_engine/exit.py
`_send_real_sell()`'s "SELL sent" Telegram message enriched. Was:
symbol + qty + reason only, no price at all. Now also includes entry price,
current stop, current target, and last-mark unrealized P&L. (The two
error-path SELL notifications — IP-blocked, Dhan-rejected — were left as-is;
those are about the rejection reason, not price.)

## 4. services/real-trade-service/execution/auto_pilot.py
`_prepick()`'s "Pre-pick" Telegram message enriched. Was: just a count
("Queued N candidates"). Now lists the actual symbols + signal price
(top 10) that were queued. Best-effort — a query failure here is caught and
logged, never blocks the pre-pick itself.

## 5. frontend/src/realTradeApi.ts
Added `dhanRegenerateToken()` → `POST /dhan/regenerate-token`, same
`rtRequest` pattern as every other call in this file.

## 6. frontend/src/components/RealAutoTrade.tsx
New "⟳ Regenerate token" button in the Dhan account card, sitting next to
the existing "Rotate token" button (same block, as requested).
- Own `regenLoading` state — doesn't freeze/disable the rest of the panel's
  buttons while its own request is in flight.
- Success shows a small green "Token regenerated ✓" note for 4s.
- Failure (including the 409 case above) surfaces through the existing
  shared error banner at the top of the panel — no new UI element needed,
  `rtRequest` already unwraps the FastAPI `detail` string into `e.message`.

**Verification done:** parsed both files with the TypeScript compiler's own
parser (syntax-only — no `node_modules`/network available to fetch
`@types/react` etc. for a full `tsc` type-check). Both are syntax-clean.
Run your own `npm run build` (which runs `tsc && vite build`) before
deploying to be sure — that will also catch anything a syntax-only parse
can't (e.g. a prop type mismatch elsewhere in the file).

## Not included in this drop
The Groww-style dashboard/pipeline redesign — deferred to next phase per
your instruction, and logged in Context Hub (STOCKKY project, entry #65)
with what's already been scoped out (`Pipeline.tsx` is currently a
decorative fake animation with no real data; `pipeline_status.py` is a real
backend tracker already wired into `RealAutoTrade.tsx` but not the main
scan dashboard).
