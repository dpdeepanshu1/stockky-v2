# Group 315 - api-gateway tests, CI for all services, env reference, and what is still open

No service logic changed in this group. It makes the safety net trustworthy, then lists what is left.

## What changed
1. **api-gateway test suite repaired (was ~20 failing, now 8603 passed).** Nobody noticed because api-gateway was not in CI and not in the routine run.
   - group302 added staggered boot warms (`GATEWAY_BOOT_WARM_DELAY_S` 8 s open / 90 s closed, `SURPRISE_BOOT_WARM_DELAY_SEC` 20 s, a surprise warm that waits up to 120 s for the movers warm).
     `tests/conftest.py` now sets those to 0 for every test and drops the process-wide `_boot_warm_movers_done` asyncio.Event (it binds to the first event loop that uses it; each test runs its own loop).
   - group221 made `/market/indices` read previous-session history after the live fetch, so `ienv.calls` is no longer exactly two entries (`test_main_market_universe_routes.py`).
   - group276 added the `DEAD_PARENT_FILL_CHECK` flag (only a literal `0` turns it off, a blank value leaves it on) - added to the sweep's allow-list in `test_env_flag_sweep.py`.
   - `test_main_surprise_routes.py` served the saved result whenever the market was closed (every weekend / after 15:30 IST); the `senv` fixture now pins the phase to "open".
2. **market-data:** two `test_group231_history_widen.py` tests expected an exception because Yahoo was unreachable in the sandbox; on a machine with internet they got data. The yfinance / NSE fallbacks are now stubbed in those tests.
3. **`scripts/docker_test.sh`** mounts the whole repo; the two env-file tests (`test_group274`, `test_group275`) read `.env.example` from the repo root, which the old services-only mount did not contain.
4. **CI (`.github/workflows/service-tests.yml`)** ran only position-stocks and real-trade. Now: all seven services, `constraints.txt` when present, `check_shared_copies.py` and `check_holiday_lists_sync.py`.
5. **`scripts/gen_env_reference.py` -> `docs/ENV_REFERENCE.md`:** every environment variable the code reads (628), the services that read it, its default and where. `--check` fails when the file is stale.
6. notification-scheduler `run_once.py`: two bare `except:` around `json.loads` -> `except (ValueError, TypeError)`.

## Still open (not done in this group)
See the list in the session reply; the short version: 2027 NSE holidays missing (deadline December 2026), sync DB commits inside `async def` in real-trade-service,
api-gateway tests make real outbound connections and one order-dependent weekday failure, 115 known differences between duplicated modules, `@app.on_event` deprecation,
Dhan depth20 / order-WebSocket layouts unverified against live data, ~400 silent `except Exception: pass`, `api-gateway/main.py` size, thin decision-prediction coverage.

Also: `test_searched_list_selfclean.py` failed (6 tests) only in a full run at weekday market hours because learned rename / delisted state left in the kv_cache memory store by another test file leaked into it; it now stubs those two loaders. The full api-gateway suite passes at four fake clocks (Sunday, Monday 10:30 IST, Monday 16:30 IST, Tuesday 00:10 IST).
