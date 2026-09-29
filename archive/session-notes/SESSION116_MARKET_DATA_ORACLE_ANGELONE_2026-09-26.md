# Session 116 — market-data-service: oracle_compat + angelone_client (2026-09-26)

## Files added

### `tests/test_oracle_compat.py` — 37 tests, 37/37 passed

Covers `oracle_compat.py` (283 lines) end-to-end. No oracledb installed;
Oracle-DDL branches driven with dialect="oracle" string directly; SQLAlchemy
sqlite in-memory for the exec_ddl_safe live execution tests.

| Class | Coverage |
|---|---|
| `oracle_is_configured` | oracle URL, postgres URL, DSN env var, empty URL no DSN, None arg |
| `dialect_name` / `is_oracle_engine` | sqlite engine, exception fallback |
| `_configure_oracle_lobs` | already-configured idempotency, sets fetch_lobs, no-module, fetch_lobs attr missing |
| `oracle_engine_kwargs` | discrete vars, full URL skips creds, ADMIN_PASSWORD fallback, pool defaults, pool overrides, TNS_ADMIN fallback, no wallet dir |
| `now_func` | oracle → SYSTIMESTAMP, all others → NOW() |
| `create_table_sql` | oracle with/without expires, postgres with/without expires |
| `create_index_sql` | oracle (no IF NOT EXISTS), postgres (IF NOT EXISTS) |
| `upsert_sql` | oracle with/without expires (MERGE), postgres with/without expires (ON CONFLICT) |
| `exec_ddl_safe` | runs valid DDL, swallows already-exists (sqlite), ORA-00955, ORA-01408, unexpected error (logged, not re-raised) |

`build_oracle_engine` and `_attach_call_timeout` are not tested — they
require a live oracle+oracledb driver and a real TNS alias. They carry
their own `# pragma: no cover` comments for the inner defensive branches.

### `tests/test_angelone_client.py` — 43 tests, 43/43 passed

Covers `angelone_client.py` (450 lines) end-to-end. All network calls
replaced by a `_FakeClient`/`_FakeResponse` pair; rate_limiter functions
monkeypatched individually.

| Class | Coverage |
|---|---|
| `get_outbound_ip` | success, exception, HTTP error |
| `_resolve_client_public_ip` | explicit env wins, cached within TTL, stale cache refreshed, fallback to 127.0.0.1 + warning log |
| `_is_rate_limit_response` | 403+exceeding-access-rate, 403+access-denied, 403+other, 429, 200, None body |
| `_safe_json` | success, exception returns None |
| `_log_denied` | 403 logs warning, 200 silent, throttled within window silent |
| `AngelOneSession.is_configured` | False when missing, True when all set |
| `_get_lock` | same lock per loop, different lock per loop (WeakKeyDictionary) |
| `_login` | raises when not configured, success sets token/feed_token/expiry, status:false raises |
| `ensure_session` | skips login when token valid |
| `get_quote` | cooldown→{}, success→first fetched, empty fetched→{}, rate-limit 403 sets cooldown |
| `get_candles` | cooldown→[], try_acquire fails→[], success→candles, rate-limit 403 sets cooldown |
| `get_gainers_losers` | cooldown→[], success, status:false logs warning, rate-limit sets cooldown |
| `get_quotes_batch` | empty tokens→[], cooldown→[], success, rate-limit sets cooldown |
| `get_session` | module singleton |

**Deprecation warnings noted** (not test failures): `angelone_client.py`
uses `datetime.utcnow()` in three places — this is a production code smell,
not a bug today, but worth updating to `datetime.now(timezone.utc)` before
Python removes the method. Flagging without changing (production code
change policy: don't touch unless it's a bug fix this session).

## No production code changed

Tests only. 80 new tests total, all passing in this sandbox.

## Next by priority

1. `bhavcopy.py` (761 lines) — NSE bhavcopy fetch/parse
2. `kv_cache.py` (1143 lines) — Neon KV store (largest remaining gap)
3. `surprise_premarket.py` (991 lines) — premarket logic
4. `main.py` (3315 lines) — FastAPI routes
