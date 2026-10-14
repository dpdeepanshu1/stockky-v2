# Group 357 - Buy Sniper daily-structure gate (WS-4)

The Hot Picks / Buy Sniper path asked "is this a good stock?" but never "is this a good place to buy it?". The HOLD / WAIT early-breakout path (conviction >= 72 and +1.2% on the day) could surface a chase, a stretched price, a setup with no room to the next resistance, or a bearish daily structure. This group asks the same question real-trade's entry gate asks, BEFORE a card is built.

The previous session started this work but it was not in the group 356 zip, so everything below was rebuilt and re-tested from scratch.

## api-gateway
- `buy_sniper.py`
  - `structure_verdict(row, price, mode)`: pure, never raises. Reads the flat `ms_*` summary (`structure_summary`, or `ms_*` keys on the row). Rules and defaults mirror real-trade's `entry_engine/structure_gate.py`: chase (1.5 ATR), stretch (3.0), room (0.5), structure R:R (1.2), `stop_too_wide`, bearish trend / fresh un-failed bearish BOS within 5 bars. Chase / stretch / room / R:R are shifted from the structure close (`ms_price`) to the card price, as real-trade does for the live price.
  - **No opinion, never block, never bullish** when: no summary, quality not ok/limited, last bar older than `STRUCTURE_MAX_AGE_DAYS`, price more than `STRUCTURE_MAX_PRICE_DEV_PCT` from the structure close (split / bad data), or no ATR.
  - `SNIPER_STRUCTURE_MODE`: `enforce` (default, the card is dropped) | `off` (kill switch, card byte-for-byte as before, no `structure` key). (group 370/371: the old `shadow` value is gone; a stale `shadow` now means enforce.)
  - Gate runs AFTER the sniper's own R:R check, so only cards that would otherwise surface are judged. The card carries a `structure` record: `gate_mode, opinion, would_reject, enforced, reasons, no_data, live, stop, target, rr`. The structural stop / target are **display only**: the card's own `stop_loss`, `target_price`, `reward_risk_ratio` are unchanged.
  - `suggestions_from_scan_payload` now also returns `structure_mode` and `structure_rejected` (`[{symbol, reasons}]`, only rows the gate itself dropped - an R:R-failed row is never blamed on structure). When the gate left nothing, `message` says so instead of the generic text.
  - `symbols_needing_structure(payload, limit)`: best-conviction buy-candidates that carry no summary (pure; `[]` when the gate is off).
  - `filter_actionable_buy_suggestions` still calls `build_suggestion` (an existing test patches it) and returns a plain list.
- `main.py`: `_sniper_attach_structure(payload)` runs before the filter in `POST /api/scan/find-buys`. The UI posts trimmed rows with no structure, so it fetches `GET {TECHNICAL_URL}/structure/{sym}` for at most `SNIPER_STRUCTURE_FETCH_LIMIT` (12) candidates, 4 at a time, inside a `SNIPER_STRUCTURE_FETCH_TIMEOUT_S` (10 s) budget, and attaches the summary in place. Fail-open: any error, bad schema or timeout leaves the row without a summary = no opinion = card unchanged. Honors `STRUCTURE_PASSTHROUGH=0`.

## Frontend
- `api.ts`: `findBuys` now forwards `structure_summary` (its field whitelist used to strip it, which would have silently made the gate blind) and types `structure_mode` / `structure_rejected`.
- `structureView.ts`: `sniperStructureView` (card badge: supports the entry / blocked / would be blocked - shadow, not applied / no structure read, plus structural stop / target / R:R and reasons) and `sniperRejectedText` ("N setups turned away by daily structure (good stock, bad entry): A, B, C +2 more"). Node tests added.
- `BuySniperModal.tsx`: structure panel per card, reward:risk row, optional `structureNote` prop (shown in the empty state and above the grid). Wired in `HotStocks`, `ScanPanel`, `SurpriseStocks`. `IpoTracker` is unchanged (IPO cards).

## Config
`SNIPER_STRUCTURE_MODE`, `SNIPER_STRUCTURE_FETCH_LIMIT`, `SNIPER_STRUCTURE_FETCH_TIMEOUT_S` (documented in `.env.example`, `docs/ENV_REFERENCE.md` regenerated). The gate reads the same `STRUCTURE_*` names as real-trade with real-trade's defaults. If the gateway shares an env file with position-stocks (which uses looser own defaults, e.g. chase 4.0), set them explicitly for the gateway.

## Tests
- `tests/test_group357_sniper_structure_gate.py` (18): every rule alone, early-breakout path, no-opinion cases, shadow / off / bad mode, zero disables a rule, live-price shift, flat `ms_*` keys, garbage input, `symbols_needing_structure`, R:R rows not blamed on structure.
- `tests/test_group357_sniper_route_enrich.py` (6): the route helper lifted out of `main.py` by AST and run against fakes (attach + block, 500 / exception / bad schema all fail open, no refetch + limit, off / passthrough-disabled fetch nothing, slow upstream cut off by the budget, route order).
- `tests/test_buy_sniper.py` (289): updated for the new card / response shape and cleared of `SNIPER_STRUCTURE_*` / `STRUCTURE_*` env.

## Verified / not verified
Run here: the three files above (via the plain runner and a minimal pytest stand-in, since pytest is not installable), `node --experimental-strip-types structureView.test.mjs`, `sync_market_structure --check`, `check_shared_copies`, `gen_env_reference --check`, a syntax-level `tsc` pass over the touched TS/TSX files (no syntax errors; a full type-check needs node_modules), and the whole api-gateway test directory run before / after through the same stand-in (identical results apart from the two new files).
NOT run: real pytest, the live `/api/scan/find-buys` route under FastAPI against a real technical service, `tsc` with real React types, `npm run build`.

## Behaviour to expect on first deploy
The gate is `enforce` by default, like real-trade. There is no record-only mode any more (group 370); `SNIPER_STRUCTURE_MODE=off` is the kill switch.
