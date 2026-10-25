# E145 - UI v7: money-aware visuals for Real Trade, Position Stocks and Real Money Calibration (2026-10-25)

Frontend only. No backend, API, switch or trading behaviour changed. Every new drawing is display-only: no request, no button, no number that animates.
Roll back: delete the `design-v7.css` import in `frontend/src/main.tsx` (the pages keep working, minus the new styling).

## What is new (and where you see it)
| Visual | Where | What it shows | Built from |
| --- | --- | --- | --- |
| **Risk rail** | Real Trade -> Positions (each card); Position Stocks -> Positions (each open row) | stop / entry / target drawn **to scale**, a live price marker that glides, and one sentence ("In profit: 46% of the way to target" / "Under water: 84% of the way to stop"). Works for a **short** too: profit is always to the right, shorts are hatched | `current_stop/target/price` (Real Trade), `stop_price/target_price/current_price/side` (Position Stocks) |
| **Exposure strip** | top of both Positions lists | one bar, each open position a block sized by money put in (entry x qty), up/down arrow per block, long vs short count, total now, "near stop" warning (70%+ of the way to the stop) | the open rows the tab already holds |
| **Capital ring** | the shared "Capital split" card (Real Trade -> Live Dhan, Position Stocks -> Overview) | the one Dhan balance split between the two services, plus the free part of the Position Stocks pool. Real Trade's share is still labelled a computed reference, as on the card | the card's own props |
| **Mode ladder** | Calibration -> Short selling, Swing lane | OFF > SHADOW > LIVE with the current rung marked; LIVE has a slow pulsing ring. Display only: moving a rung stays on the Switches tab | `shortModeView` mode, swing `kind` |
| **Evidence meter** | Calibration -> Swing lane | closed trades x/30 and entry days y/20 as two meters; the slower one decides | `swingView().progress` |
| **Drift beat** | Calibration -> Reconciliation (Real Trade) | heartbeat line: calm pulse = in step, rough = drift found, flat red = check did not finish, dashed = no report; always also written as a word | `reconcileView().kind` / warnings |
| **Armed glow** | hero of Real Trade (REAL mode, armed only) and Position Stocks (armed; still red ring when the loss kill switch has tripped) | a slowly turning ring round the header | existing `armed` / kill-switch state |
| Smaller | `SHORT` chip now has an arrow and hatching; position cards get a green (long) / red-hatched (short) left edge; KPI tiles get a hover sheen | | |

The Real Trade card's old risk bar split stop and target 50/50 whatever the entry was; it is replaced by the true-scale rail (the one visible behaviour change).

## Safety rules kept (same as v5/v6)
Money numbers never animate; direction is always an arrow or word too (never colour only); motion is transform/opacity only; every loop and entrance is off under reduced-motion, TV mode and the existing "Animations: Off" switch, loops pause on a hidden tab; entrances use fill-mode `backwards`.
One trap found and fixed while checking in a browser: a bare `shadow` class on the ladder is a Tailwind box-shadow utility, so the mode classes are prefixed `v7m-`.

## Files
New: `design-v7.css`, `v7View.ts` (pure helpers), `components/ui/{RiskRail,ExposureStrip,ModeLadder,EvidenceMeter,CapitalRing,DriftBeat}.tsx`, `v7.test.mjs`, `design-v7.test.mjs`.
Changed: `main.tsx` (import), `PageHero.tsx` (optional `glow` prop), `RealAutoTrade.tsx`, `realAuto/parts.tsx`, `realAuto/tabs/PositionsTab.tsx`, `PositionStocksTab.tsx`, `CapitalSplitCard.tsx`, `calibration/{ShortSellingPanel,SwingPanel,ReconcilePanel}.tsx`, `package.json` (`npm test` runs the two new suites).

## Checked here
- `tsc --noEmit` clean; `npm test` all suites pass (2 new: 10 logic tests, 10 design guards); real `npm run build` succeeds.
- Mutation checks: removing one "Animations: Off" switch, or animating `width` in a keyframe, fails `design-v7.test.mjs`.
- Chromium screenshots of every new component with sample data: dark, light, 390 px mobile; no console errors.

## NOT checked / still open
- Not clicked through on the real pages with live data (needs your VM). Open Auto Trade -> Positions, Position Stocks -> Positions and Overview, Calibration -> Short selling / Swing lane / Reconciliation once.
- The exposure strip and rail only draw when stop and target exist; a carried CNC position (bracket cancelled) shows no rail on purpose.
- Real Trade positions are all longs today, so the rail there is passed `side="LONG"`; if Real Trade ever gets shorts, pass the side through.
