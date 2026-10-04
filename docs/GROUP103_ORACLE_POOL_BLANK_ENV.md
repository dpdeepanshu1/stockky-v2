# group103 (2026-10-04) - kv_cache / Oracle pool settings: a blank value aborted engine creation

Cumulative on group102. Rebuild everything that carries these modules: `docker compose build api-gateway market-data-service analysis-intelligence-service real-trade-service position-stocks-service decision-prediction-service notification-scheduler-service && docker compose up -d`.

## Cause (found by the kv_cache pool-settings audit noted after group77)
`oracle_compat.oracle_engine_kwargs` did `int(pool_overrides.get(name, os.environ.get(NAME, default)))`. A variable that is SET but EMPTY (a compose `${VAR:-}` line, a bare `CACHE_DB_POOL_SIZE_ORACLE=` in `.env`) comes back from `os.getenv` as `""`, not the default, so `int("")` raised `ValueError` and engine creation failed. `kv_cache.py` passed `os.getenv("CACHE_DB_POOL_SIZE_ORACLE", os.getenv("CACHE_DB_POOL_SIZE", "5"))` into it, so a blank `_ORACLE` variable also skipped the shared `CACHE_DB_POOL_SIZE` fallback. The same function serves the hot-picks, surprise and IPO schema engines. Group75/76 made the direct `int()`/`float()` env reads blank-safe; this path read its values through the override argument and was missed.

## Fix
- `oracle_compat.py` (all 8 byte-identical copies, still identical): each value resolves override -> env var -> default, and a BLANK entry counts as unset and falls through. A non-blank value that is not a number still raises (the existing pinned test `test_non_numeric_pool_value_raises` stays valid, so a real typo is not hidden).
- `kv_cache.py` (6 copies): the Oracle branch reads `CACHE_DB_POOL_SIZE_ORACLE`, then `CACHE_DB_POOL_SIZE`, then 5 (same for overflow, with default 3; recycle 300; timeout 10), treating blank as unset at each step.
- Defaults are unchanged. The Neon/Postgres branch was already blank-safe and is untouched.

## Audit notes (no change)
- The Postgres pool is hard-capped at 2 + 1 regardless of env (documented in the code for Neon's free tier), so `CACHE_DB_POOL_SIZE` above 2 has no effect there. Left as designed.
- The three `kv_cache.py` variants differ only in service-specific durable key prefixes and the gateway's stale-read helper, as intended.

## Not verified
I have not seen which of these variables are blank on your VM. If none are, this changes nothing at runtime.

## Tests
`api-gateway/tests/test_oracle_pool_blank_env.py` (10): defaults, numeric override, blank/whitespace/None override, override -> env var -> default order, blank env vars, typo still raises, kv_cache source reads blank-safe, all `oracle_compat.py` copies byte-identical. Suites run here: api-gateway 8111, market-data 690, analysis-intelligence 2161, real-trade 2948 (1 skipped), decision 38, prediction 20, training 38; position-stocks 2411 passed and notification-scheduler 177 passed, each with 1 failure that also fails on the untouched group99 code (position-stocks `test_dhan_client::test_valid_creds_returns_client` needs the `dhanhq` package, not installed in this sandbox; notification-scheduler `test_telegram_long_message_split` gets 6 parts where the test expects at most 4).
