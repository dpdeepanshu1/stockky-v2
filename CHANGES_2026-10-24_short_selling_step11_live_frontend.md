# Short selling step 11: frontend for live shorts - E100

Position Stocks only. Longs look exactly as before (no badge, Buy / Sell, same P&L formula for a blank or LONG side).

## What changed
- `frontend/src/positionSide.ts` (new, pure): `sideOf`, `isShort`, `legLabels` (long Buy / Sell, short Sell / Cover), `pnlPctFromPrices` (short = entry - exit), `sideBadge`, `shortModeView` (off / shadow / live wording).
- `PositionStocksTab.tsx`: a red **SHORT** badge on an open or closed short card and in Trade History; the card labels the legs Sell / Cover for a short; the closed P&L % fallback is short-aware (it was long-only). The live unrealized P&L was already short-aware in the service.
- Position Stocks `main.py`: the positions list and the trade history now send `side` (`LONG` | `SHORT`; a blank or unreadable value shows as LONG).
- Short selling panel (Real Money Calibration) and the Board card: they read the real mode. In live they say **LIVE / real orders** instead of "no order is placed", the live headline explains where live shorts show, and the one-click button's confirm text explains that an open short keeps its broker stop and is covered by 14:55 (Close all covers it now). The panel still has exactly one control that sets a switch: `SHORT_MODE` to `off`. Raising to shadow or live, and raising `SHORT_LIVE_QTY_CAP`, stay on the Switches tab, which already asks to confirm a loosening (the page's switch list is built from the catalog, so the new `live` choice and the cap appear there without extra code).
- `orders/short_entry.summary()` reports the configured mode, so the page can tell live from shadow (the shadow `mode()` still reads live as off).
- `positionSide.test.mjs` (7 tests) added to `npm test`.

## Checks
`npm test` passes (all 16 files). Python: `test_short_live.py` script 30 OK, catalog and shared-copy checks OK. Not run: `tsc` / `npm run build` (no `node_modules` here), the position-stocks pytest suite, any FastAPI route.

## Still open
Item 10 (exit tests against the real exit code, needs real pytest on the VM), item 12 (tighter stop for stressed and panic shorts, and the `SHORT_MARKET_STATE_OFF` reason in the candidate log), and the Stockky history/charges tabs were not changed (the charges tab already uses each order's real BUY/SELL leg).
