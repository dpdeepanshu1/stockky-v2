# E162 - IPO Tracker: row faces get the v7 look, and "undefined" cells become dashes (2026-10-25)

Includes E161. Third card-body screen (plan order: Hot Picks, Surprise, IPO, Data Feed, Training, then Paper Trades, Watchlist, Alerts, Settings). Status: `docs/AUDIT_STATUS_E162.md`.
Roll back: in `frontend/src/components/IpoTracker.tsx` restore the old row face (the `stageLabel` function above `BusySpinner`, `const stage = stageLabel(ipo)` and `gainPct`, the plain Issue / CMP / gain / "Score n/100" / GMP lines) and drop `ipoRowView` from the import; delete `ipoRowView` and its types from `components/ipoView.ts`; delete the E162 block at the end of `design-v7.css`; remove the "E162" blocks from `ipoView.test.mjs`.

## Why this one is a fix, not only a look
The old row printed raw fields: a row with no issue price showed "Issue ₹undefined", an upcoming IPO with no date showed "Lists undefined", a listed row with no day count showed "Day undefined", and a current price of 0 showed "CMP ₹0". All are now a dash, a plain sentence ("Listing date not set", "Listed") or left out. A real 0 gain stays "0.0%" and "Day 0" stays "Day 0".

## What you will see (each IPO row, collapsed face)
- Stage line as before (Lists date, pre-open, listing day live, Day n, awaiting data, data error) with the same wording.
- Left edge bar: green = listing day live, amber = pre-open or awaiting data, red = data error, grey otherwise (the stage line says the same in words).
- Gain vs issue: a ▲ or ▼ in front of the signed number; 0.0% has no arrow.
- Score: a chip with number and a word (Strong / Good / Mixed / Weak) and a spoken label, same thresholds as the Decision ring.
- GMP: ▲ / ▼ in front, with sign; still hidden once the stock trades.
- Unchanged: decision badge, advisory text, the Buy / Scan-for-Buy button, Detail toggle and the whole detail panel, the lifecycle overview and strip above, the feed health panel below.

## Files
`frontend/src/components/ipoView.ts` (+ `ipoRowView`, pure), `frontend/src/components/IpoTracker.tsx` (row face only; unused `stageLabel` removed), `frontend/src/design-v7.css` (static rules, no animation), `frontend/src/ipoView.test.mjs` (extended, still 44 suites).

## Tests
Run here: `npm test` passes (44 suites, exit 0). Row view covered for full, bare, zero, negative, hidden-GMP and unknown-stage rows; three deliberate breakages caught (price 0 shown, GMP shown after listing, "Lists undefined" back). esbuild bundles the screen; strict `tsc` on `ipoView.ts` alone passes. NOT run: `tsc --noEmit` on the whole app, `vite build`, a browser, backend pytest.
