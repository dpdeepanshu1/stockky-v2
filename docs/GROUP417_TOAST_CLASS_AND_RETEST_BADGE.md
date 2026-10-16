# Group 417 - toast style clash fixed, retest badge on the decision card

Frontend only. No backend, config or env change.

## 1. Toast clash
`design-v3.css` had a global `.fixed.top-4.right-4 { background ... !important; border ... }` rule (plus a mobile `top/right/left` rule with the
same selector). It was meant for the `App.tsx` status toast but also matched the `Trades.tsx` success / error toast, flattening its green / red tints.
The `App.tsx` toast now has its own class `app-toast`; both CSS rules target `.app-toast`. `Trades.tsx` is untouched and keeps its own styling.

## 2. Retest badge
The decision `structure` block returns `retest_entry` (zone names that excused an old BOS; null on a normal pass). `structureView.ts` gets
`retestBadge()` (label e.g. `Retest: OB / FVG`, plain-word tooltip) and `DecisionStructure.retest_entry`; `DecisionCard.tsx` shows it at the top of the
structure block. It only appears when `STRUCTURE_RETEST_ENTRY` is on (default off), so nothing changes today. Test: `structureView.test.mjs`.
Not covered: the Buy Sniper card, whose record (real-trade structure snapshot) carries no `retest_entry`.

## Verified / not
`node --experimental-strip-types src/structureView.test.mjs` passes. `tsc && vite build` NOT run (no node_modules here): run `npm ci && npm run build`.

## Checked and left alone (from the review)
Compose wiring of `STRUCTURE_RETEST_ENTRY` / `BOARD_USE_MS_ENGINE` is not needed: every service loads `env_file: .env`.
Entry / target tick rounding: VM log showed 0 hits for 16283 / "tick size", so unchanged.
