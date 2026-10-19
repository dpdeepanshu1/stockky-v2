# Group 461 - login redesign, phases 3 to 5 (presentation only)

No API call, state, handler, order or money logic changed. Frontend only: rebuild the `frontend` image.
Roll back: remove `design-v4.css`'s "PHASE 3" and "PHASE 5" sections, and the `GlintBox` / `PriceRoll` wrappers (each is a plain element underneath).

## Phase 3 - the three trading pages
- REAL / DEMO frame (`.mode-frame`, design-v4.css): a thin frame plus a labelled ribbon on Real Auto Trade (follows the DEMO / REAL switch: "REAL MONEY - live Dhan orders" in red, "DEMO - simulated orders, no money" in indigo), Position Stocks ("REAL MONEY - live Dhan scalp orders") and Real Money Calibration ("REAL MONEY - these switches change live trading"). The ribbon spells the mode out, so it is never colour-only.
- Motion wired into live price rows: `PriceRoll` on the Current / LTP price and `GlintBox` (edge flash for 700 ms when the price changes) on the position cards in Real Auto Trade (auto-pilot card, Dhan open positions) and Position Stocks (`PositionRow`). Only PRICES are animated; P&L, value, quantity and charges never are (a test enforces it).

## Phase 4 - the rest of the app (the parts that show live prices or load)
- Market Movers: skeleton tiles instead of a pulsing block, price roll + glint on each card, cards are keyboard-operable (Tab, Enter, Space); the hover-only "Analyse" button is out of the tab order.
- Market Sentiment header: skeleton instead of a pulsing block.
- Surprise Stocks table: price roll on the price column (group 462). Watchlist symbol tiles: lazy painting (group 462).
- Trades list rows: `content-visibility: auto` (`.v4-lazy`) so a long history is not laid out until scrolled near.

## Phase 5 - accessibility, devices, performance
- Visible keyboard / remote focus ring on every control; 40 px tap targets on touch screens, 44 px on phones; the tab bar scrolls sideways on narrow screens.
- Reduced motion, TV mode and a hidden tab all switch the new motion off (guard test); high-contrast users get a solid frame edge.
- TV mode: no backdrop blur, heavier frame and bigger ribbon.
- Performance: `will-change` only while a price is rolling, lazy painting of long lists.

## Tests
`frontend/src/design-v4.test.mjs` (in `npm test`): off switches for every animation, keyframes touch only non-layout properties, every `.mode-frame` names its mode and (for REAL) says REAL MONEY, the Real Trade frame follows the switch, motion helpers never get a money value. All 5 pass; the five older frontend tests still pass.

## Not run here
`npm ci`, `tsc` against React types and `vite build` (no node_modules / network in this sandbox). Every edited .tsx file passes a syntax-only `tsc` check. Run `bash scripts/docker_frontend_check.sh` on the VM, then look at the three pages on a phone and a TV once.

## Left as is on purpose
The small spinners inside IpoTracker / IpoFeedHealth buttons mean "this action is running", not "content is loading", so a skeleton does not fit; they stay.
