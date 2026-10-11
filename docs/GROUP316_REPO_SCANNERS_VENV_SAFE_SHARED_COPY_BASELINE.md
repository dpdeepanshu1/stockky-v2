# Group 316 - repo scanners no longer read the virtualenv; shared-copy check fixed; CI runs both

No service logic changed. Tooling and CI only.

## What was wrong (found when `gen_env_reference.py` was run on the VM)
`python3 scripts/gen_env_reference.py` printed **752 variables** instead of 628. The 124 extra rows were `APPDATA`,
`ALLUSERSPROFILE`, `COVERAGE_CORE`, `CFFI_TMPDIR`, ... read by pip / coverage / platformdirs inside
`services/real-trade-service/.venv/lib/python3.12/site-packages/`. The scanner only skipped a few folder names, so a local
virtualenv was read as if it were production code. Three scripts shared the flaw:

| Script | Effect of the flaw on a machine with a `.venv` |
|---|---|
| `gen_env_reference.py` | 124 foreign variables in `docs/ENV_REFERENCE.md`; `--check` differs per machine, so it can never gate CI |
| `check_price_source_imports.py` | any installed package that imports yfinance shows up as a "NEW direct yfinance import" |
| `check_shared_copies.py` | any `rate_limiter.py` / `notifier.py` inside a package would be counted as a copy of ours |

## Fixes
1. **`scripts/_repo_walk.py` (new):** one rule for "not our code": caches, `node_modules`, `site-packages`, build output, any
   `.venv*` / `venv*` / `env` folder, and **any folder that contains a `pyvenv.cfg`** (catches a venv with an unusual name).
   All three scripts use it. Output is now identical on the VM, a laptop and CI.
2. **`check_shared_copies.py` was already failing on the uploaded zip** (exit 1, 97 "changed again" entries), and group 315 had
   just put it in CI, so the very first CI run would have been red. Two causes:
   - the baseline hashes were stale (copies were edited the same way everywhere, `--update` was never run);
   - the hash used `ast.dump`, which is not stable across Python versions (3.12 adds `type_params=[]`, 3.13 drops empty
     fields), and CI runs 3.11 while the VM runs 3.12. A baseline written on one would fail on the other.
   Now the hash comes from a small canonical writer (`_canon`) that skips version-only and empty fields. Before refreshing the
   baseline I checked that nothing really drifted: same 115 symbols, same grouping of identical copies for every one of them.
3. `gen_env_reference.py`: the printed count was `newlines - 8` (breaks if the header changes); it now prints `len(found)`.
4. **CI (`service-tests.yml`)** repo-checks job now also runs `gen_env_reference.py --check`,
   `check_price_source_imports.py` and `scripts/test_repo_scripts.py`; the workflow triggers on those files and on
   `docs/ENV_REFERENCE.md`.
5. **`scripts/test_repo_scripts.py` (new, 5 tests):** a fake venv full of third-party env reads must not appear in the
   env reference or the yfinance check; the canonical hash ignores version-only fields; the committed
   `ENV_REFERENCE.md` and shared-copy baseline are current.

## How it was checked
- Real tree plus a fake `services/real-trade-service/.venv/.../platformdirs/windows.py`: old script 629 variables (leaks
  `ALLUSERSPROFILE`), new script 628, `--check` passes.
- `check_shared_copies.py` exit 0, `check_price_source_imports.py` exit 0, `check_holiday_lists_sync.py` exit 0.
- `pytest scripts/test_repo_scripts.py`: 5 passed.

## On the VM
`docs/ENV_REFERENCE.md` in your working tree is the 752-variable file. After merging this zip run
`python3 scripts/gen_env_reference.py` once (or just take the zip's copy); it will print `628 variables`, and `--check` will pass.

## Still open (unchanged from group 315)
2027 NSE holiday dates (December 2026), sync DB commits inside `async def` in real-trade-service, api-gateway tests that make
real outbound connections, `@app.on_event` deprecation, Dhan depth20 / order-WebSocket layouts unverified against live frames,
~400 silent `except Exception: pass`, `api-gateway/main.py` size, thin decision-prediction coverage.
