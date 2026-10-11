# Group 314 - URL encoding, utcnow, money-path logs, shared-copy check

Finishes the small items left over after group 313. No order logic changed.

## What changed

1. **Symbols are percent-encoded in URL paths** (position-stocks): `feed/bar_backfill.py` (`/history`), `screening/prev_day.py` (`/history`),
   `screening/quality_gate.py` (`/analyze`, `/analyze`, `/events/.../categorized`), `orders/depth_gate.py` (`/quote`, `/depth`).
   `urllib.parse.quote(symbol, safe='&')`: `&` stays literal (so `M&M` is exactly the URL it was), `/ ? # space` are encoded.
   Test: `tests/test_group314_symbol_url_encoding.py` (also a static guard against a raw `{symbol}` in those URLs).
2. **`datetime.utcnow()` removed** (deprecated in Python 3.12, naive-datetime trap). 25 files: decision-prediction, market-data, position-stocks `feed/angelone_session.py`,
   real-trade `scripts/historical_backtest_calibration.py`, and the tests that used it. The replacement `datetime.now(timezone.utc).replace(tzinfo=None)` is the same naive-UTC
   value, so no comparison or stored value changes. (analysis-intelligence already had its own `_utcnow()` helper; its docstring mentions were left.)
3. **Silent `except: pass` on money paths now log** (the cleanup / rollback guards stay silent on purpose):
   - `orders/eod_squareoff.py` - cancel of a super-order leg (3 sites): INFO with the leg, order id, symbol and error.
   - `real-trade-service/portfolio/portfolio.py` - an unreadable broker position row is skipped with a WARNING (was invisible).
   - `entry_engine/opening_guard.py` - failure to read the shared not-before time: WARNING, still falls back to the configured time.
   - `auth/dhan_credentials.py` - the Telegram notice after a TOTP refresh / refresh failure could not be sent: WARNING (secrets redacted).
   - `adaptive_thresholds.py` - stale-threshold notice not sent: WARNING (retried next boot). `candidate_engine/candidates.py` - history persist skipped: DEBUG.
   - `entry_engine/entry.py` - traded value unavailable (fail-open): DEBUG.
4. **Shared-copy drift check**: `python3 scripts/check_shared_copies.py` (see its docstring). `oracle_compat` and `boot_forensics` must be byte-identical
   (`--sync <family>` copies the first over the others). `kv_cache`, `rate_limiter`, `circuit_breaker`, `pipeline_status`, `notifier` legitimately differ per service; the
   current differences are the baseline in `scripts/shared_copies_baseline.json`, and any NEW or changed difference fails the check. `--report` lists them; `--update` accepts them.
5. **Root clutter**: `production_fixes_round2.patch`, `production_fixes_round3.patch` -> `archive/old-patches/`; `tier_breakdown.csv` -> `archive/data/` (the one doc that names it was updated).

## Not changed
- `api-gateway/main.py` (14,271 lines) is not split: a mechanical split of 139 route handlers sharing module state is a refactor that needs the live app to verify.
- ~400 other `except Exception: pass` sites (rollback / close / cleanup guards) are left.
- Dependency pinning still needs `scripts/freeze_constraints.sh` on the VM (needs docker).
