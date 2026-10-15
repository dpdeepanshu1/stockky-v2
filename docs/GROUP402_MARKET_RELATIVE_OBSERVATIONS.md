# Group 402 - market-relative returns on the real-trade and position-stocks structure observations

## Why
`structure_observations` (real-trade) and `scalp_structure_observations` (position-stocks) record what the daily structure gate saw and what the
stock did over the next 1/5/10/20 sessions. The edge report compared PASSED with REJECTED raw returns, so a market-wide rally lifted both sides and
could hide, or fake, a gate edge. Group 401 did this for the decision service; this group does it for the two trading services.

## What changed
- `shared/market_structure/observation.py` (synced to the five `stockky_ms` copies): `bench_outcome(obs_ts, bars)` returns `bench_ret_{1,5,10,20}d`
  on the same basis as `ret_{h}d` (close of the h-th session after the observation day), measured from the benchmark close ON the observation day.
  The stock's entry price is intraday, so this is an approximation of a few tenths of a percent. No bar on the observation day, or fewer than h
  sessions after it = `None` (never 0). `aggregate()` adds `excess` (return minus market, rows with both values only) per verdict and `edge_excess`
  (passed minus rejected after taking the market out). Existing keys are unchanged.
- Both services: four nullable `bench_ret_*` columns on the observation model; existing tables get them at boot (position-stocks via
  `_COLUMN_MIGRATIONS`, Oracle `BINARY_DOUBLE`; real-trade via `_ensure_structure_obs_bench_columns`, Postgres/SQLite `DOUBLE PRECISION`, Oracle `BINARY_DOUBLE`).
  A failed ALTER is logged ("could not add ...") and never stops the service booting.
- Evaluators: `evaluate_pending(..., fetch_bench=None)`. With neither `fetch` nor `fetch_bench` given (production), the benchmark named by
  `STRUCTURE_OBS_BENCH_SYMBOL` is fetched ONCE per pass (120 days) through `fetch_daily_bars`. A benchmark failure leaves the fields empty and the
  stock's own outcome is still stored; a later pass never blanks a value an earlier pass stored. Callers that pass their own `fetch` (older tests) get no benchmark fetch.
- Reports: `/structure/edge` (position-stocks) and the real-trade report show `by_verdict.*.excess` and `edge_excess`; recent rows carry `bench_ret_*`.

## Config
`STRUCTURE_OBS_BENCH_SYMBOL` (default `NIFTY`; empty = no benchmark) in both services; `.env.example` and `docs/ENV_REFERENCE.md` updated.

## Not done / to check after deploy
- Not run against a real database (no sqlalchemy here): look for "could not add structure_observations.bench_ret_*" in the real-trade log and run `bash run_tests.sh`.
- Rows evaluated before this group have empty `bench_ret_*`; they are filled on the next pass only while the row is still incomplete (a completed row is not revisited).
- Whether market-data serves the name `NIFTY` on `/history` in production is not verified here (the service maps NIFTY* names and falls back to `^NSEI`).
