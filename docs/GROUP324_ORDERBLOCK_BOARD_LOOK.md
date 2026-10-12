# Group 324 - Market Board restyled to the order-block.com look

Frontend only (`frontend/src/components/board/`). No backend, API or behaviour change: the board still places no order, polls
`GET /board` the same way, and the hosts (Position Stocks, Real Trade: `marks`, `hatchFor`, `showZoneScore`, `legend`, `popupExtra`)
work unchanged.

## What changed
- **Look**: one dark "terminal island" (`board.css`, every rule scoped under `.ob`, stays dark in the light theme): grid backdrop,
  neon bull / bear tiles with gradient fill by percentile, glowing top-6 ranks, window-dot title bar, big bullish / bearish counts
  with an up / flat / down gauge, Live pill (pulsing while live).
- **Flow lanes** (`FlowLines.tsx`, `flowGeometry.ts`): each stock is a curve from its own sector row in the rail, through a thin glowing
  bus, into its tile. Pulses travel along the 40 strongest curves while the market is live. Curves draw in once; later 2 s refreshes
  redraw instantly (no replay). Static curves are cached offscreen; 30 fps, paused when the tab is hidden, capped at 250 curves.
- **Tiles** (`BoardTile.tsx`): outer group = position (slides on rank change), inner group = entrance animation; price tick flashes the
  tile; depth bars breathe while live; big tiles carry a sparkline, rank and conviction; long symbols are clipped to the tile.
- **Sector rail**: diverging change bar per sector; row height shared with the curve origins.
- **Empty / weekend state**: animated placeholder tiles with a clear "Placeholder tiles - not market data" note. Last session's board is
  still shown (pill "LAST SESSION <day>") when one was logged.
- **Popup** (`StockPopup.tsx`, `ZoneChart.tsx`): initials badge, big price, day sparkline, key-value grid (rank, conviction dots, VWAP, OB,
  zone, S1 / R1, zone score, R:R, chase, day open), rule badge, candle chart with price tags, IST time axis and animated candles.
  `extra` (host content) and footnotes stay outside the dark card, in the app theme.
- `prefers-reduced-motion`: all animation and transitions off, curves drawn once.

- **Around the board**: the Position Stocks journal cards (P&L by day, win rate, shadow study) and the Real Trade "positions and
  candidates" panel (`RealAutoTrade.tsx`, chips only, logic untouched) use the same dark card; the app's `text-mist/paper/signal-*`
  helper classes are mapped to the island tokens inside it. The Real Trade risk-cap box in the popup stays in the app theme.

- **Header**: the leading / lagging sector now carry a small sparkline (mean of that sector's tile sparklines, each rebased to % move;
  drawn only when the rows have sparks, so a last-session snapshot shows none) and the big bullish / bearish counts ease to new values
  (instant under reduced motion).

- **Audit fixes**: the phone/desktop layout switch is decided from the whole board width (before, boards ~520-700 px wide flipped
  between the two layouts every frame because showing the rail shrank the stage); on a phone the sector rail is a row of chips above
  the board, so the sector filter still works there.

## Tests
`node --experimental-strip-types frontend/src/components/board/flowGeometry.test.mjs` (Node >= 22.6): lane / rail / curve geometry,
placeholder layout, header pill states, heat, conviction dots, IST clock. `scripts/docker_frontend_check.sh` now runs it.

## Checked here / not checked here
- Checked: the 4 node tests pass; every changed `.tsx` parses; the board was bundled with the real React and rendered in headless
  Chromium against mock data (live, closed with nothing ranked, last session, popup): no page errors.
- Not run: the project's own `npm ci && tsc && vite build` (no network in the sandbox, so Tailwind and `@types/react` were not
  available). Run `scripts/docker_frontend_check.sh` on the VM. Colours in the screenshots came from mock data, not your feed.
