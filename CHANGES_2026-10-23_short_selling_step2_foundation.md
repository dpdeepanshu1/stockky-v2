# Short selling, Part 2 step 2: the foundation (23 Oct 2026). Position Stocks only. BUILT, nothing reads it, no short can exist.

Defaults change nothing for any long trade: no entry, exit, size, gate, stop, P&L or charge code was touched. Real Trade is untouched and stays long-only (a test pins it).

## What was added (position-stocks-service)
- `models.py` / `db.py`: `scalp_positions.side`, `String(5)`, NOT NULL, default `"LONG"`; idempotent ALTER with `DEFAULT 'LONG' NOT NULL` for existing rows (same pattern as the other defaulted columns; both dialect defaults set). Only the position table got it. Nothing reads it yet. (The `side` values `bull`/`bear` on the board tables are a different, unrelated field.)
- `config.py`: `SHORT_MODE` = `off` (default) or `shadow`. **`live` is deliberately not accepted yet**: `live`, junk and empty all read as `off`. `live` is added in the step that builds the exit side.
- `control/catalog.py`: `SHORT_MODE` on the calibration page, choices `off` / `shadow` only, shipped `off`.
- `calibration-runner-service/autocalib.py`: `SHORT_MODE` is on the PROTECTED list; the drift test now also covers `SHORT_*` keys, so a later short switch cannot be left loose by accident.

## Tests
`position-stocks-service/tests/test_short_foundation.py` (8, text/ast + clean-interpreter config, no sqlalchemy needed): column NOT NULL default LONG; only the position table; migration entry; `SHORT_MODE` off/shadow/live/junk; catalog has no live choice; protected from the calibrator; **nothing reads `SHORT_MODE` anywhere**; Real Trade has none. Also passing: `test_group477_catalog_matches_config`, the runner's autocalib / regime / learner tests (65).

## Checked on the internet before building (23 Oct 2026)
- **Dhan Super Order** (dhanhq.co/docs/v2/super-order): `transactionType` is `BUY` or `SELL`, with `targetPrice` and `stopLossPrice`; the only example is BUY (target 1600, stop 1400). **No SELL rule is documented.** S0 is therefore still open and the plan's fallback (plain MARKET SELL + service-managed BUY stop and target) stays in the design.
- **Static IP:** Dhan says order placement / modify / cancel (including super orders) need a whitelisted static IP. You already trade through the API, so this should already hold; a short adds nothing new here.
- **Dhan equity intraday auto square-off is 3:10 PM** (Dhan support page). This repo squares off at `EOD_SQUAREOFF_TIME_IST=15:00`, ten minutes earlier, so our own close runs first. Keep it that way for shorts: do not move EOD past 15:10. I did not find Dhan's charge for its own auto square-off; ask in S0.
- **SEBI:** retail short selling is allowed but naked short selling to delivery is not, so an intraday short must be closed the same day (SEBI short-selling framework as reported by Moneylife; an old SEBI discussion paper says the same). That framework also names F&O-eligible stocks as the eligible set. Whether Dhan restricts MIS shorts to F&O stocks is NOT confirmed: add it to S0 and, until answered, build the later gates to prefer F&O-eligible symbols.
- **Retail algo framework** (SEBI, fully mandatory 1 April 2026): static IP, unique strategy IDs; under about 10 orders per second counts as ordinary API use. Nothing in it blocks shorting that I found. A forum post says one broker refused API market orders under an NSE circular; that is a single unverified comment, and your exits already send MARKET SELL on longs, so market orders work on your account today. Whether a MARKET SELL that OPENS a short is accepted is exactly what the 1-share test shows.

## Not done / next
- NOT run: the position-stocks pytest suite (no pytest / sqlalchemy here). Run `python3 -m pytest tests/test_db.py tests/test_short_foundation.py -q` on the VM, and check boot logs show `added missing column scalp_positions.side` once.
- `test_e37_catalog_coverage.py::test_exempt_list_has_no_stale_entries` FAILS, and fails the same on the E91 zip: its EXEMPT list still names `SHORTLIST_ENABLED`, `ADAPTIVE_HISTORY_ENABLED`, `DHAN_ORDER_WS_ENABLED`. Not caused by this change and left alone.
- S0 is still unanswered, so nothing in step 3 onward should go live. Next is step 3, the EXIT side (`eod_squareoff.py`, `reconcile.py`, `exit_retry.py`, `close_all.py`, `stop_watchdog.py`, `overnight_stop.py` skipping shorts, P&L and charges): the biggest and riskiest set, about 280 places assume SELL. It should be built in slices, one file at a time, each with its own tests.
