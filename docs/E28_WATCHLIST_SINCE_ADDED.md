# E28 - "% since added" on the watchlist (2026-10-22), display only

`frontend/src/watchlistGain.ts` (pure helpers) + `components/WatchlistManager.tsx`. Each watchlist card now shows e.g. `+31.74% since added` under the symbol (green up, red down, grey flat, `●` and bold at +/-5 % or more, hover shows the baseline price).

* **Baseline** = the `/quote` price at the moment you add the symbol. It is kept in this browser (`localStorage` key `stockky.watchlist.added.v1`), so it is per device, not synced to the cloud list.
* **Symbols already on the list** get no true baseline: the first price the app sees becomes the baseline and is labelled `since first seen`. Adding a symbol explicitly replaces a first-seen guess; removing a symbol forgets its baseline.
* **Live price**: `/quote` for up to 40 symbols, refreshed every 60 s while the watchlist is open.
* No trading, scanning or backend change. The earlier plan note that the backend already stored a price at add was wrong: `catalyst_price` belongs to real-trade candidates, not the user's watchlist.

## Not done
* The notification when a stock moves by a set percent (the second half of plan item 28): needs the baseline on the server so it can fire with the app closed.
* A server-side baseline (so it follows you across devices).
* Verification of the component: `tsc` / `vite build` were not run (no node_modules, no network). The helpers have tests (`src/watchlistGain.test.mjs`, run, passing) and the existing `design-v4`, `alignment`, `structureView` and other node tests still pass. `calibration.test.mjs` was already failing in the uploaded zip (its expected job list is out of date) and still does; it is earlier in `npm test` than the new test, so `npm test` stops there until it is fixed.
