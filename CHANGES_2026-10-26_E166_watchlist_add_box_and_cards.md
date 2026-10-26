# E166 - Watchlist: the Add box refuses names and duplicates with a reason; cards show direction and "no price yet" (2026-10-26)

Includes E165. Seventh card-body screen (plan order: ..., Paper Trades, Watchlist, then Alerts, Settings). Status: `docs/AUDIT_STATUS_E166.md`.
The Watchlist already had the v5 summary (WatchlistPulse) and v5 cards with safe percent maths (`watchlistGain.ts`), so this change is small and targeted.
Roll back: in `frontend/src/components/WatchlistManager.tsx` restore the old `add()` (`const sym = (symOverride ?? input).trim().toUpperCase().replace(/\.NS$/i, "").replace(/\.BO$/i, ""); if (!sym || symbols.includes(sym)) { setInput(""); return; }`), remove the `note` state and the `v7-wl-note` line, restore `onChange={(e) => setInput(e.target.value.toUpperCase())}` and the card text `{pct === null ? "—" : `${formatPct(pct)} ${label(added[s])}`}`; delete `checkSymbol` and `SymbolCheck` from `watchlistGain.ts`; delete the E166 block at the end of `design-v7.css`; remove the "E166" block from `watchlistGain.test.mjs`.

## Bugs fixed
- **The Add box accepted any text** (a pasted company name, spaces, punctuation, "<b>") and saved it to the watchlist that scans and alerts run on. It now accepts an NSE code only: letters, digits, "&" and "-" (TCS, M&M, BAJAJ-AUTO, 3MINDIA), up to 20 characters, after stripping ".NS" / ".BO". A refused entry stays in the box so it can be fixed, with the message "Use the NSE code (letters and digits, for example TCS or M&M), not a name."
- **A duplicate was silently cleared.** It now says "TCS is already on your watchlist." Quick-add buttons go through the same check.
- **A symbol with no price showed a bare dash.** The card line now says "no price yet" (the hover text already did).
- **Direction on the card line** is now ▲ / ▼ plus the signed percent, not colour and sign only (the summary tiles already had arrows).

## Unchanged
Summary tiles, "since added / since first seen" maths and the 60 s refresh, Scan watchlist, Remove, Analyse by clicking the symbol, the empty state, the cloud save.

## Files
`frontend/src/watchlistGain.ts` (+ `checkSymbol`, pure), `frontend/src/components/WatchlistManager.tsx`, `frontend/src/design-v7.css` (one static rule), `frontend/src/watchlistGain.test.mjs` (extended, still 44 suites).

## Tests
Run here: `npm test` passes (44 suites, exit 0). Three deliberate breakages in the helper and one in the component caught (pattern loosened, duplicate check removed, ".BO" strip removed, Add bypassing the check). esbuild bundles the screen; strict `tsc` on `watchlistGain.ts` alone passes. NOT run: `tsc --noEmit` on the whole app, `vite build`, a browser, backend pytest. Not checked: whether the backend also validates symbols on save.
