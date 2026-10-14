# group357 - live_quotes writers get their own DB pool (log review item #1)

Source: the 14 Oct 10:38 IST restart log, item 1 (High) of the pasted log-issue list.

## What the log showed
- `dhan live_quotes write: 0 of 1 rows written (last error ... QueuePool limit of size 1 overflow 1 reached, connection timed out, timeout 10.00)`
- `dhan creds: trade_credentials read failed: TimeoutError` (item 2, same cause)
- `DPY-4024 call timeout of 8000 ms exceeded` (96 of 97 rows) (item 2)

## Cause (read from the code)
`angelone_ws_feed._get_live_quotes_engine()` returned `kv_cache._get_neon()` - the ONE durable engine of market-data-service. That
single pool also served every kv read/write, the `/live-quote` database fallback, and `dhan_data/creds.py` (the Dhan token read,
through the same `_get_neon`). Two background writers (the AngelOne feed writer and the Dhan writer) hold a connection for a whole
MERGE chunk; with a 1+1 pool one slow chunk (or two writers at once) left nothing for the credentials read, so it timed out on the
pool, not on Oracle.

## Change
- `market-data-service/angelone_ws_feed.py`: `_get_live_quotes_engine()` now returns a dedicated engine, built once on the same
  database (same URL / wallet / DSN / `ORACLE_CALL_TIMEOUT_MS` through `oracle_compat.build_oracle_engine`), with its own pool.
  Both writers (`angelone_ws_feed` and `dhan_data/live_store.py`) go through that function, so both move. Reads, kv and the
  credentials read stay on the shared engine and are no longer starved by writes.
- If the dedicated engine cannot be built the shared engine is used and the build is retried after 60 s (never raises).
- If the shared engine is replaced (tests, re-init) the dedicated one is rebuilt for it.
- `dhan_data/live_store.status()` now has an `engine` block (`kind` = dedicated | shared, `separate_enabled`, `error`), visible in
  the existing Dhan status output.

## Switches (blank = default; all in `.env.example` and `.env.oracle.recommended`)
| Variable | Default | Meaning |
|---|---|---|
| `LIVE_QUOTES_DB_SEPARATE_ENGINE` | 1 | 0/false/off/no = share the KV engine (old behaviour) |
| `LIVE_QUOTES_DB_POOL_SIZE` | Oracle 2, Postgres 1 | Oracle 1-8; Postgres capped at 2 |
| `LIVE_QUOTES_DB_MAX_OVERFLOW` | Oracle 1, Postgres 0 | Oracle 0-8; Postgres capped at 1 |
| `LIVE_QUOTES_DB_POOL_TIMEOUT` | 10 | seconds to wait for a pool connection |
| `LIVE_QUOTES_DB_POOL_RECYCLE` | `CACHE_DB_POOL_RECYCLE` or 300 | seconds |

Cost: up to 3 extra Oracle sessions (2+1) from market-data-service. If you see ORA-12520 "no more sessions", lower
`LIVE_QUOTES_DB_POOL_SIZE` or set `LIVE_QUOTES_DB_SEPARATE_ENGINE=0`. Neon free tier: 1 extra connection by default.

## Also in this zip
- `.env.example` / `.env.oracle.recommended`: `DHAN_POLLER_ALWAYS_SYMBOLS` was read by the code but documented nowhere (two
  group274 tests were failing on that).
- `tests/test_group344_live_path.py::test_failed_rows_are_not_remembered_as_sent`: failed since group347 because the timeout in the
  test opens the Oracle back-off; the test now resets `_backoff_until` before the retry (test bug, not a code bug).
- `docs/ENV_REFERENCE.md` regenerated (`gen_env_reference.py --check` clean).

## Tests
`tests/test_group357_live_quotes_own_pool.py` (12): dedicated engine used, built once, switch off, blank switch, build failure
falls back and retries, no database, rebuild on new shared engine, Oracle pool env (blank-safe), Oracle defaults 2+1, no URL,
Postgres cap, status block.

## Not changed (still open from the same log list)
Items 2 (Oracle latency itself), 3 (api-gateway sync rollback on the event loop), 4 (dynamic universe add/drop suffix
contradiction), 5-19. Item 4 is the next one by priority: trading-side effect (dropped symbols still queued).
