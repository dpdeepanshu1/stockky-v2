# Group 339 - clickable picks, candle/line chart, live momentum

## What changed (frontend only, plus two test/lint fixes)
- **Top picks cards and Best picks rows are clickable** (Position Stocks Board tab and Real Trade Board tab). Click, Enter or Space opens the same stock popup the board tiles use.
  Works for any pick, including ones the Market Board ranks bearish ("shorted") - the popup is loaded by symbol from `GET /board/{symbol}`.
- **Candles / Line toggle** in the popup (`ZoneChart` `mode` prop). Zones, S/R levels, VWAP, opening range and held-position lines show in both. The choice is remembered in the browser.
- **Live momentum strip** in the popup (`board/momentum.ts`, pure + tested): last 3 / 6 bars %, vs VWAP, green bars of last 5, position in the day range, last-bar volume vs average,
  and one label (Strong up / Up / Flat / Down / Strong down) with a -100..+100 score. Refreshes every 5 s with the popup.
- **Popup footer for a card** says what the auto-buy engine does with the pick: First in line / Reserve / Entered / Board preview.
- Bearish-side stocks show a note that the engines only buy long.

## Auto-buy wiring (verified, not changed)
- position-stocks: `shortlist.build` -> `quality_passers` -> entry loop tries pick #1 first, falls through to the next pick on a symbol-specific skip.
- real-trade: `_shortlist.build(approved_entries)` -> first `ENTRY_MAX_NEW_PER_CYCLE` picks are placed, reserve picks take over if a placement fails.
- The cards read that same shortlist, so "First in line" on a card is the stock the engine tries first. Every entry gate and risk check still decide. Nothing here guarantees profit.

## Fixes
- `orders/window_health.py`: the silent `except: pass` is now a debug log (it tripped `test_group322_no_new_silent_excepts`).
- `tests/test_group336_calendar_catalyst_window.py::test_filter_candidates`: the fixture had one window only, which the code (by design) never pauses; added a profitable 15m window and a cache reset.

## Run
- `python3 -m pytest -q tests` in each service; `npm ci && npm run build` in `frontend`;
  `node --experimental-strip-types src/components/board/momentum.test.mjs` (Node >= 22.6).
