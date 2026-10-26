# E163 - Price providers set to Dhan -> AngelOne -> yfinance; Data Feed price-alert card (2026-10-25)

Includes E162. Status: `docs/AUDIT_STATUS_E163.md`.

## Part 1 - which source do IPO Tracker, Hot Picks and Surprise Momentum use?
Checked in the code (not on your VM):
- **All three already ask market-data-service first**, and market-data decides the provider from `QUOTE_PROVIDER_ORDER` / `HISTORY_PROVIDER_ORDER`: Surprise (`/quotes/bulk`, `/quote`, `/history` for baselines), IPO (`/history`, `/quote`), Hot Picks (analysis `technical` -> market-data `/history` and `/quote`; repair via `/quotes/bulk`). The code default of that order is already `dhan,angelone,yfinance`.
- **What was wrong:** both shipped env files (`.env.example`, `.env.oracle.recommended`) set `angelone,yfinance,dhan`, i.e. Dhan **last** and yfinance **before** Dhan (the old "shadow rollout" order). Anyone who copied them got that order. Both files now say `dhan,angelone,yfinance`.
- Direct yfinance calls inside the gateway / analysis service stay a last resort only (`GATEWAY_DIRECT_YFINANCE_FALLBACK`, `ANALYSIS_DIRECT_YFINANCE_FALLBACK`, both default on), reached only when market-data has no answer. Unchanged.
- A failed Dhan call (no Data API subscription, expired token, open breaker) falls through to AngelOne, then yfinance, on its own.
- New guard: `scripts/test_provider_order_shipped.py` (shipped env files list dhan, angelone, yfinance; the code default matches).

**Needs your VM:** your live `.env` may still carry the old order, and the change only takes effect there after you edit it and restart market-data-service. Check what is serving: `GET /internal/data-sources` and `GET /internal/dhan-status` on market-data-service (Dhan needs the Data API subscription and a stored token). Remaining direct-yfinance files are listed in `scripts/price_source_allowlist.txt` (gateway `main.py`, `surprise_*.py`, `ipo_scanner.py`, analysis `technical/main.py`, training); none of them runs before market-data.
Not changed (your call): `YAHOO_WS_FEED_ENABLED=1` and the AngelOne live feed still run next to Dhan; with Dhan first, `/quote` and `/quotes/bulk` ask Dhan before them, but the Data Feed "live quote feed" line still reports the Yahoo socket.

## Part 2 - Data Feed: Price Alerts card
- Adding an alert now checks the target first: `Number("abc")` was NaN and went to the backend as null. A target that is not a number above 0 shows "Target price must be a number above 0."; a blank symbol shows "Enter a symbol." (commas, spaces and a leading ₹ are accepted).
- Each alert row shows a direction glyph and word (▲ above / ▼ below) with a left edge bar (green above, red below), the target as ₹ (a dash when missing, never "₹undefined"), and "fired n×". Remove and Check now unchanged.

Roll back: put the two `*_PROVIDER_ORDER` lines in both env files back to `angelone,yfinance,dhan` and delete `scripts/test_provider_order_shipped.py`; in `DataFeed.tsx` restore the old add handler (`target_price: Number(alertTarget)`) and the old row (`{a.symbol} {a.direction} ₹{a.target_price}`), drop `checkAlertTarget` / `alertRowView` from the import and from `feedView.ts`; delete the E163 block at the end of `design-v7.css`; remove the "E163" blocks from `feedView.test.mjs`.

## Files
`.env.example`, `.env.oracle.recommended`, `scripts/test_provider_order_shipped.py` (new), `frontend/src/components/feedView.ts`, `frontend/src/components/DataFeed.tsx`, `frontend/src/design-v7.css`, `frontend/src/feedView.test.mjs`.

## Tests
Run here: `npm test` passes (44 suites, exit 0); `pytest scripts/test_provider_order_shipped.py` passes (5), and fails when the order is put back (checked). `feedView.test.mjs`: four deliberate breakages caught. esbuild bundles the screen; strict `tsc` on `feedView.ts` alone passes. `scripts/env_review_check.py` on both files: ok. NOT run: market-data pytest (the code default did not change), `tsc --noEmit` on the whole app, `vite build`, a browser.
