# Session 21 — Full Position Stocks Tab Audit

**Requested:** "audit position stock tab fully"

## Scope
Full field-by-field cross-check of `services/position-stocks-service/main.py`
against `frontend/src/positionStocksApi.ts` and
`frontend/src/components/PositionStocksTab.tsx` (all 8 sub-tabs), plus a
fresh line-by-line read of the whole 1490-line tab component.

## Bugs found and fixed (2 real, 1 type-accuracy)

1. **Stale window count in Overview header.** `PositionStocksTab.tsx` still
   read "Position Stocks — 5m / 15m / 60m Scalp Pool" even though the 1m
   window was added in session 4 and is used everywhere else on the tab
   (window filter buttons, screener grid, pipeline stage labels). Fixed to
   "1m / 5m / 15m / 60m Scalp Pool".

2. **Hardcoded position-capacity denominator.** The Positions tab's
   "Open Positions (N/5)" header hardcoded `/5` instead of reading
   `status.max_concurrent_scalp_positions` (env-configurable via
   `MAX_CONCURRENT_SCALP_POSITIONS`, default 5) — the Pipeline tab's own
   "Entry Decision" stage already does this correctly two sections over.
   Fixed to `{openPositions.length}/{status?.max_concurrent_scalp_positions ?? 5}`.

3. **Type drift (no runtime effect today).** `ScalpTradeHistorySummary`'s
   `best_trade`/`worst_trade` types in `positionStocksApi.ts` were missing
   `opened_at`, which `GET /trades/history` has actually returned for a
   while. Added the field to the type so it matches the real response
   shape.

## Verification
- First session with real npm registry egress from the sandbox: ran an
  actual `npm install && npm run build` (not just an isolated `tsc` parse
  against stub types) — **zero errors/warnings** both before and after the
  fixes.
- `python3 -m py_compile` on every `.py` file in position-stocks-service:
  clean.
- `python3 -m pyflakes .` on the whole service: clean.

## Not changed
Every other backend route (`/status`, `/positions`, `/trades/history`,
`/candidates`, `/candidates/log`, `/ledger`, `/dhan/live-orders`,
`/dhan/account`, `/cycle/run`, `/ws-status`) was cross-checked field-for-field
against its frontend type and consumer — all already correct, no changes
needed. Charges tab fee math, capital ledger card, 5-stage pipeline
visualization, arming sequence, and all button `disabled=` gating logic
re-read and confirmed correct.
