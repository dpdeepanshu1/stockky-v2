# E120 - UI v5: calmer finance look, AI orb, AI Insights deck (presentation only)

No API call, state, handler or business logic changed.

- `frontend/src/design-v5.css` (new) - cards, hero glow, buttons, search focus, AI orb, AI Insights deck. Imported after v4 in `main.tsx`.
  **Rollback:** delete that import line (and the `<AiInsightDeck>` line in `DashboardView.tsx`).
- `frontend/src/components/ui/AiOrb.tsx` (new) - decorative CSS orb (idle / thinking / done) and a small typing indicator.
- `frontend/src/components/app/AiInsightDeck.tsx` (new) - dashboard sub-section. Idle: 3-step "how it works". Scanning: live progress. Results: Buy / Prepare / Hold / Avoid counts and top 3 picks (tap opens the stock). Reads existing props only.
- `frontend/src/design-v5.test.mjs` (new, added to `npm test`) - motion off-switches (reduced-motion, TV, hidden tab), transform/opacity-only keyframes, import order, mount.

Not run here: `tsc && vite build` (no node_modules offline). Syntax of all touched TSX checked; design-v4 and v5 tests pass.
Run `cd frontend && npm install && npm run build && npm test` on your side.

## Part 2
- `components/ui/MoodGauge.tsx` (new) + mounted at the bottom of `MarketSentimentHeader.tsx`: fear-to-greed bar with a needle, always labelled in words (Fearful / Cautious / Neutral / Optimistic / Greedy). Uses the existing `market_score`; no new request.
- `design-v5.css` §7-9: gradient brand mark with a slow shine, calmer sidebar links, Hot Stocks cards rise in one after another. All have reduced-motion / TV / hidden-tab off switches (covered by `design-v5.test.mjs`).

## Part 3 - stock result card
- `components/ui/ScoreRing.tsx` (new): circular AI score that draws itself once; number plus a word (Strong / Good / Mixed / Weak), never colour-only.
- `components/ui/TradePlanBar.tsx` (new): stop loss, buy zone and target on one bar, the current price marked, and the risk : reward figure. Shows only for BUY NOW / PREPARE TO BUY and only when stop < entry < target; otherwise renders nothing.
- `DecisionCard.tsx`: one small block (`v5-hero-extra`) inside the hero plus two imports. No data, handler or existing markup changed.
- `design-v5.css` §10-11 and test coverage in `design-v5.test.mjs`.
Rollback: remove the `v5-hero-extra` block and the two imports in `DecisionCard.tsx`.

## Part 4 - Trades, Real Auto Trade, Position Stocks, real-money banner
- `components/ui/MoneyModeBanner.tsx` (new): one shared banner that says in words where the money is: PAPER TRADING (Trades), DEMO MODE / REAL MONEY (Real Auto Trade, follows its existing DEMO / REAL switch), REAL MONEY (Position Stocks). Safety chips carry a symbol (✓ ⚠ ✕): auto-orders armed / off, risk setting confirmed, loss kill switch ready / TRIPPED, trades saved / database offline.
  It only reads state each page already had. It has no buttons, never changes the mode and never renders a money amount.
- Mounted with one block + one import each in `RealAutoTrade.tsx`, `PositionStocksTab.tsx`, `Trades.tsx`. Existing `mode-frame` ribbons are unchanged.
- `design-v5.css` §12-13: banner, stat cards (accent edge, hover lift), gradient title icon, softer mode switch / buttons, sticky table headers with row hover on all tables.
- Money rule kept: no number on these pages animates; only the red "live" ping dot moves, with reduced-motion / TV / hidden-tab off switches.
- `design-v5.test.mjs`: mounted on all three pages, mode spelled out, symbols on chips, no ₹ / formatting / PriceRoll in the banner, banner cannot change mode.
Rollback: remove the three `<MoneyModeBanner>` blocks and imports.
Note (unchanged behaviour, worth knowing): `RealAutoTrade` starts in REAL mode (`useState<Mode>("REAL")`). The banner now makes that obvious; I did not change the default.

## Part 5 - Watchlist and mobile bottom bar
- `components/WatchlistPulse.tsx` (new): four tiles above the symbol grid: how many stocks tracked, how many are up / down since added, best mover, weakest mover (tap opens the analysis). Percentages only, never money; arrows and signs beside the colour.
- `WatchlistManager.tsx`: mounts the pulse; "Quick add" chips (shown while fewer than 5 symbols) call the same `add()` as the Add button (it now accepts an optional symbol; the Add button was changed to `onClick={() => add()}` so the click event is never passed in); symbol cards get an avatar and a left edge in the move's direction (the signed percentage text is unchanged).
- `design-v5.css` §14-15: styles above, plus a springy active tab on the phone bottom bar (icon pop, sliding pill, small dot). Reduced-motion / TV off switches; covered by `design-v5.test.mjs`.
Rollback: remove the `<WatchlistPulse>` and quick-add blocks and the `v5-wl-*` additions in `WatchlistManager.tsx`.

## Part 6 - IPO Tracker
- `components/IpoLifecycle.tsx` (new, no state, no requests): "IPO overview" at the top of the page. Upcoming > Pre-open > Listing day > Listed with a count each (stages that are live today are tagged "now"), a line for IPOs still waiting for data, the three biggest listing moves (percent with arrow and sign; tap opens that row), and the AI orb, which spins with a % while a scan runs.
- `IpoTracker.tsx`: one import and one `<IpoLifecycle list={ipoList} scanning={ipoScanning} progress={ipoProgress} onPick={setExpandedSymbol} />` line. Nothing else changed.
- `design-v5.css` §16 plus test coverage (mounted, no requests/state, stages in words, off switches).
Rollback: remove that line and the import.
