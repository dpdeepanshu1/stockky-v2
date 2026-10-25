# E161 - Surprise Momentum: table rows get the v7 look, and "NaN" cells become dashes (2026-10-25)

Includes E160. Second card-body screen (plan order: Hot Picks, Surprise, IPO, Data Feed, Training, then Paper Trades, Watchlist, Alerts, Settings). Status: `docs/AUDIT_STATUS_E161.md`.
Roll back: in `frontend/src/components/SurpriseStocks.tsx` restore the old `stocks.map` body (cells used `Number(s.x).toFixed(...)`, the `isBuilding` pill classes and the `/100` pill) and drop `surpriseRowView` from the import; delete `surpriseRowView` and its type from `components/surpriseView.ts`; delete the E161 block at the end of `design-v7.css`; remove the "E161" blocks from `surpriseView.test.mjs`.

## Why this one is a fix, not only a look
The old cells ran `Number(x).toFixed()` on every value. A missing change % showed "NaN%" (or a silent "+0.00%" when the value was null), a missing RVOL showed "NaNx", a missing target or trailing stop showed "₹NaN", a missing score showed "undefined/100". All are now a dash. A real 0 stays 0 (the F-11 rule).

## What you will see (each row of the table)
- Score: a chip with the number and a word (Strong 70+, Good 55+, Mixed 40+, Weak), same thresholds as the Decision ring; a spoken label for screen readers.
- Tier: "▲ Breakout" or "◆ Building" with a glyph, so it no longer relies on colour.
- Change %: a ▲ or ▼ in front of the signed number; 0.00% has no arrow.
- Row edge: a thin bar on the left, green for up, red for down, grey for flat or missing.
- Unchanged: columns, headings (Target +5%, Trail (VWAP)), symbol button, price (still the existing price roll, prices only), RVOL and slope, trigger, empty and scanning messages, everything above the table.

## Files
`frontend/src/components/surpriseView.ts` (+ `surpriseRowView`, pure), `frontend/src/components/SurpriseStocks.tsx` (row cells only), `frontend/src/design-v7.css` (static rules, no animation), `frontend/src/surpriseView.test.mjs` (extended, still 44 suites).

## Tests
Run here: `npm test` passes (44 suites, exit 0). Row view covered for full, empty, zero and clamped rows; three deliberate breakages caught (₹ guard removed, Building glyph removed, direction forced up). esbuild bundles the screen; strict `tsc` on `surpriseView.ts` alone passes. NOT run: `tsc --noEmit` on the whole app, `vite build`, a browser, backend pytest.
