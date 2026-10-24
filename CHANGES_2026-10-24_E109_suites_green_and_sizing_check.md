# E109 - every suite run for real and green; the "higher of plan entry and fresh price" check (24 Oct 2026)

## P0 item 6 closed (code side): all suites run where the dependencies exist
position-stocks 4233 passed | real-trade 5286 passed, 1 skipped | calibration-runner 224 passed | api-gateway 9044 passed | frontend `npm test` green, `npm run build` (tsc + vite) OK | repo guard scripts OK.
(api-gateway needed `sgmllib.py` from sgmllib3k copied in by hand here because that package will not build on Python 3.13; the VM image is unaffected.)

## Stale tests fixed (no production code changed)
The router split (E76-E86) moved handlers out of `main.py`; five tests still looked for them there, and one depended on a pydantic detail:
- `test_group358_prepare_structure_notes.py` (6 tests): finds `refresh_prepare_to_buy` in `gateway_support/bound_routes_f.py`, dedents it and gives it a stand-in `ref`.
- `test_group361_sniper_observation.py::test_tick_action`: the `sniper-observe-eval` action is in `bound_routes_d.py`.
- `test_surprise_premarket.py` cross-module contract: also reads `bound_routes_k.py` imports.
- `test_gateway_support_split_d.py`: reads the `limit` bound from `.metadata` when the FieldInfo has no `.le` (pydantic 2.11, the pinned version).

## Plan check: "risk uses the higher of the plan entry and a fresh price" - no bug
- real-trade: `OrderIntent.entry_price` is built from the fresh tick (`tick.price` plus half the entry zone), and the risk engine measures `|entry - stop|` on it. Not stale.
- position-stocks long: quantity is sized on the scan price, but the stop and target are re-anchored to the live tick at the same stop % (E-2026-10-22 `live_anchor`), and the price guard refuses a fill more than `ENTRY_MAX_SLIPPAGE_PCT` (0.25%) above the signal. So the rupee risk can exceed the budget by at most about 0.25%. Not worth touching the live long path.
- live short (`short_live`): sized on the live price already.
