# Group 324 — UI refresh (frontend only)

No backend, trading, or API code changed. Everything below is in `frontend/`.

## A real bug found first
`tailwind.config.js` defined the themed colours (`ink`, `graphite`, `slate`,
`mist`, `paper`) as plain `var(--x)` strings. Tailwind cannot apply an opacity
modifier to those, so classes such as `text-mist/60`, `border-slate/40`,
`bg-ink/40` (~600 uses, 98x `text-mist/50` alone) were never generated and the
elements fell back to inherited colour / default borders. Colours are now
RGB-channel variables with `<alpha-value>`, so they work in both themes.
Expect some secondary text, borders and tinted backgrounds to appear for the
first time — that is the original intent of the code.

## What changed
- **Design tokens** (`index.css`, one definition per theme instead of three
  overlapping ones): accent blue = interactive/selected, buy/sell = gain/loss,
  warn amber = caution only. Amber used to be both the brand colour and the
  warning colour, so a warning looked the same as "selected". Light-theme
  colours tuned to WCAG AA (>= 4.5:1) on background and panel; dark-theme solid
  buttons use dark label text (`--on-solid-rgb`) because white on those fills
  was about 2:1.
- **Type**: one family (Inter, tabular figures). "Mono" never meant source code
  here, it meant "numbers that line up", so `--font-mono` is Inter with tabular
  numerals. Sizes of 8-11px (about 700 uses) are lifted one step; ALL-CAPS
  labels and wide letter-spacing become normal case. Real code still has
  `--font-code`.
- **Shell**: sidebar grouped as Trading / Discover / System with inline SVG
  icons (emoji removed); new top bar with search, live-price / backend /
  database status in plain words, IST clock, theme toggle (replaces the
  uppercase status strip, whose "TAB X" pill duplicated the sidebar).
- **Mobile**: bottom bar is now Home, Auto Trade, Scalp (Position Stocks),
  Picks, More — the two trading services were not reachable from the bar before.
  Drawer is grouped the same way. Form fields are 16px on phones so iOS does
  not zoom on focus. Tap targets >= 44px for tabs/inputs.
- **Pages**: every tab opens with a title and one line saying what it is for
  (`components/ui/PageHeader.tsx`). Dashboard leads with "Analyse any stock";
  scan buttons are "Quick scan" / "Full market scan".
- **One tab control** (`components/ui/SubTabs.tsx`) replaces two different
  hand-rolled bars in Auto Trade and Position Stocks; counts are badges.
  Auto Trade's column widened from 768px to 1024px.
- Shared styles for buttons, tables, badges, forms, toast, command palette,
  focus rings (`:focus-visible`) and `prefers-reduced-motion`.

## Not changed on purpose
Component internals (RealAutoTrade / PositionStocksTab bodies, cards, data
wiring), any trading behaviour, API calls.

## Verification
- `npx tsc --noEmit` clean; `npm run build` clean (no CSS warnings).
- Contrast ratios computed for every text/background pair in both themes.
- NOT verified: visual rendering. No browser was available in the sandbox, so
  nothing was screenshotted. Please eyeball Dashboard, Auto Trade (Overview,
  Positions), Position Stocks, and the mobile drawer in both themes.
  The most likely visual surprises: the newly-working `/NN` opacity classes,
  and text-size lift in dense tables on narrow phones.

## Files
`frontend/tailwind.config.js`, `frontend/index.html` (theme-color),
`frontend/src/index.css`, `frontend/src/App.tsx`,
`frontend/src/components/RealAutoTrade.tsx`,
`frontend/src/components/PositionStocksTab.tsx`,
new `frontend/src/components/ui/{NavIcon,SubTabs,PageHeader}.tsx`.
