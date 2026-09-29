# Session 118 — market-data-service: surprise_premarket.py (2026-09-26)

## File added

### `tests/test_surprise_premarket.py` — 62 tests, 62/62 passed

Covers `surprise_premarket.py` (991 lines) comprehensively. No yfinance,
no Neon, no real SQLAlchemy network. DB paths use SQLite in-memory engines
or monkeypatched `_db_url`. All `precalculate_surprise_baselines` paths
tested without real threads via monkeypatched helpers.

| Class | Coverage |
|---|---|
| `_normalize_db_url` | postgres→postgresql, channel_binding strip, sslmode=required→require, sslmode added, not doubled |
| `_db_url` | no env, ORACLE_DSN set, oracle:// URL, CACHE_DATABASE_URL, fallback chain |
| `_yahoo_sym` | .NS/.BO strip + .NS append, lowercase, empty, None |
| `ensure_schema` | no DB URL, delegates to surprise_schema, falls back when surprise_schema fails, SQLAlchemy connection error |
| `compute_baseline_for_symbol` | INDEX_SKIP, caret symbol, yfinance missing, empty history, valid history → full row dict, zero high_52w, exception |
| `compute_baseline_from_bhavcopy` | valid data, <5 rows, zero high_52w, missing symbol, exception, .NS suffix stripped |
| `_table_exists` | existing table (graceful), missing table |
| `upsert_baselines` | empty rows, no DB URL, connection error |
| `_write_progress` / `get_premarket_progress` | write-then-read, idle when no file, idle on corrupt file, write error swallowed |
| `bulk_baselines_from_bhavcopy` | no DB URL, no symbols, connection error, table missing |
| `default_universe_from_env` | comma-separated, semicolon-separated, SCAN_UNIVERSE fallback, fallback universe (250 symbols), whitespace strip |
| `precalculate_surprise_baselines` | already_running lock, lock released on completion, schema_failed, empty symbols, dedup+normalize, bhav_rows path (source_bhavcopy), yf_rows path (source_yfinance), residual per-symbol fallback, residual errors counted, MAX_SYMBOLS cap, unexpected exception → error dict + lock released, progress file written on done |

**Bugs confirmed caught:**
- `_FakeTicker()` without `__init__(self, sym)` → `compute_baseline_for_symbol` returns None
  (yfinance's `Ticker(sym)` passes the symbol argument; stub must accept it)
- `_write_progress` / `get_premarket_progress` use the module-level `_PROGRESS_PATH`
  constant, not `os.getenv()` dynamically — must monkeypatch `sp._PROGRESS_PATH`, not the env var
- `compute_baseline_from_bhavcopy`: with `high=0 + i*0.1`, `close=0 + i*0.1`, SQLite
  ORDER BY DESC returns the most-recent row first (highest i), so `prev_close > 0`
  and the guard doesn't fire — truly zero-value test needs all-zero fixed values

## Session totals

62 new tests, all passing. 384 total across all 8 market-data-service test files.

## Next by priority

`main.py` (3315 lines) — the final and largest gap in market-data-service.
Likely needs 3-4 rounds given its size.
