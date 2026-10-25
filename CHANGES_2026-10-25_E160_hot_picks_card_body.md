# E160 - Hot Picks: card body gets the v7 score ring and trade-plan bar (2026-10-25)

Includes E159. First card-body change after the summary strips (plan order: Hot Picks, Surprise, IPO, Data Feed, Training, then Paper Trades, Watchlist, Alerts, Settings). Status: `docs/AUDIT_STATUS_E160.md`.
Roll back: in `frontend/src/components/ConvictionCard.tsx` remove the two imports (`ScoreRing`, `TradePlanBar`), the four lines after `collectReasons` (`scoreNum`, `showRing`, `shownPx`, `showPlan`), the `cc-v7-hero` block, and the `!showRing &&` wrapper around the Score tile; delete the E160 block at the end of `frontend/src/design-v7.css`; remove the "E160: card body guards" block from `frontend/src/hotView.test.mjs`.

## What you will see (every Hot Picks card, all three sections)
- A score ring (number plus a word: Strong / Good / Mixed / Weak) replaces the plain "Score /100" tile. The ring draws once; the number never animates.
- For a **BUY NOW** or **PREPARE TO BUY** pick, the same stop loss / buy zone / target bar the Decision card uses, with the live price marked and "Risk : reward 1 : x". It is not drawn for HOLD or DO NOT BUY, and not when the stop, entry or target is missing or out of order (the bar then draws nothing).
- Everything else on the card is unchanged: decision pill, data-quality badge, Price, Conf, the Entry / Target / Stop loss / Horizon text row (kept as the words for the drawing), score chips, blurb, reasons.
- A score of 0 gets a ring ("Weak"), not a dash. A card with no score keeps the old tile showing a dash.

## Files
`frontend/src/components/ConvictionCard.tsx` (only Hot Picks uses it), `frontend/src/design-v7.css` (one static layout rule, no animation), `frontend/src/hotView.test.mjs` (7 source-level guards added to the existing suite, still 44 suites).

## Tests
Run here: `npm test` passes (44 suites, exit 0). Two deliberate breakages were caught (buy-zone gate removed; truthiness test instead of finite test). esbuild bundles the card; strict `tsc` with a React stub reports nothing on the new lines. NOT run: `tsc --noEmit` on the whole app, `vite build`, a browser.
