# E111: Time Machine (full port), backend to frontend

Position Stocks only. EVIDENCE ONLY: it places, sizes and blocks nothing, writes nothing to the database, and nothing reads its answer.
Closes the plan's "full 10,000-replay port with NSE session and cost model" item (E103's recorder stays as the in-trade, 1-minute version).

## The question it answers
"If I open this stock at this time, LONG or SHORT, with this stop % and target %, and must be flat by the square-off, how often does the target come first, and what is the average result in R after charges and slippage?"

Two independent answers, shown side by side:
- **Real history**: the exact rule on every complete recent 5-minute session of the stock (market-data `/history`, 1 month, or an exact 58-day window with `TIME_MACHINE_LOOKBACK=2mo`).
- **Simulation**: 10,000 (switch, 1,000 to 50,000) made-up sessions. Every 5-minute slot draws a real bar *shape* (gap, high, low, close against its own open) from the same time of day, chained slot by slot. It keeps the stock's busy-open / quiet-middle volatility, forgets any trend or reversal inside a day.
The opposite side is run on the same made-up sessions as a control (a rising window makes a long look good and a short look bad; the control shows that).

Break-even aware: a 2:1 target only has to be reached about 1 time in 3 before charges; the real Dhan charge model (`orders/charges_ledger.charges_for_trade`, short sells first) and slippage (default 0.05% per market leg, none on the target limit leg) raise that number and it is shown. Verdict codes: FAVOURS, AGAINST, NEAR_BREAKEVEN, MIXED (simulation and history disagree), NOT_ENOUGH_DATA. Simulation votes with a +/-0.05 R band; history only votes with 10 or more trades at that entry time (+/-0.10 R band).

Fixed conventions: entry fills at the open of the entry bar; a bar touching both levels counts as the STOP; stop and target fill at their levels; a trade still open at the flat-by time (15:00 long, 14:55 short, from the existing square-off settings) is closed at the last bar's close.

## Backend (position-stocks-service)
- `timemachine/engine.py`: pure core, stdlib only (parse, walk, replay, pools, synthetic sessions, summaries, Wilson interval, R histogram, verdict).
- `timemachine/data.py`: market-data fetch with a 10-minute cache (64 stocks), 2mo-to-1mo fallback, `DataError`; a failed fetch is an error, never a guessed bar.
- `timemachine/service.py`: validation, charges from the ledger model, one answer (`run`), up to 15 stocks ranked (`compare`, 5,000 sessions each), `status`. At most two runs at once (a third gets HTTP 429).
- Routes in `main.py`: `GET /time-machine/status`, `POST /time-machine/run`, `POST /time-machine/compare` (read-only, same openness as `/short/shadow`; the two-run limit and the caps keep them cheap).
- Switches (Evidence recording group, live, read at call time, on the calibrator's PROTECTED list): `TIME_MACHINE_ENABLED`, `TIME_MACHINE_SIMS`, `TIME_MACHINE_SLIPPAGE_PCT`, `TIME_MACHINE_LOOKBACK`. `TIME_MACHINE_DEFAULT_VALUE_RS` (20,000) and `TIME_MACHINE_FETCH_TIMEOUT_S` (25) are env-only.

## Frontend
- New Position Stocks sub-tab "Time Machine" (`components/TimeMachinePanel.tsx`): form (symbol, side, entry time, flat-by, stop %, target %, trade value, made-up sessions, history window), verdict with the plain-language gap and control lines, tiles (simulated target-first with its 95% range, needed to break even, averages in R, stopped / still-open shares, spread of one trade, charges), net-R histogram, entry-time table, day-by-day history, honest limits, and a compare table.
- `timeMachineView.ts` (pure helpers, same limits as the server so a bad value never costs a round trip) and `timeMachine.test.mjs` (added to `npm test`).
- `positionStocksApi.ts`: `timeMachineStatus / Run / Compare`.

## Fix carried over from the unfinished session
The last session ended with one red test: `test_default_charges_fn_matches_the_ledger_when_it_can_be_imported` called `default_charges_fn()` (which imports SQLAlchemy) before its import guard. The guard now comes first. Nothing from that session was in the E110 zip, so everything above was rebuilt.

## Proof (what was run here)
- `tests/test_time_machine.py`: 35 tests, all pass (stand-in runner; the module is stdlib only). Covers parsing, the walk (long, short, both-levels = stop, flat-by), history, synthetic sessions (valid bars, deterministic per seed, a rising pool always hits the long target and always stops the short, a dead-flat pool is all flat), verdict codes, cache / expiry / fallback, errors raised and not guessed, parameter limits, charges from the model, the full answer shape, busy (429), switch off (503), compare ranking and failed list, no order or database code in the module, the three routes and four switches exist and are PROTECTED.
- `scripts/check_control_catalog.py`: ok. `scripts/gen_env_reference.py --check`: up to date. `test_e37_catalog_coverage`: 3/3.
- `timeMachine.test.mjs` and the rest of `npm test`: pass. `timeMachineView.ts` type-checks under `--strict`.

## Not run here (run on the VM)
- `python3 -m pytest tests -q` in position-stocks-service (pytest and SQLAlchemy are not installable here); the one test that compares the charge function with the ledger skips itself without SQLAlchemy and will really run there.
- `scripts/docker_frontend_check.sh` (npm ci, tsc, vite build): the panel was only checked for syntax and as far as TypeScript can go without `node_modules`.
- A live run against market-data to see how many 5-minute sessions it really serves. If it serves few, the page says "Not enough data" and names the count; `TIME_MACHINE_LOOKBACK=2mo` asks for more.

## Limits (also on the page)
One to two months of one market mood; one stock; neighbouring entry times are not independent; the simulation has no trend or reversal inside a day, no overnight gap and no volatility regime; fixed % levels, not Stockky's ATR levels; no order book, queue or circuit effects. A reality check and a filter, never a forecast. Using it as a gate is still P3.
