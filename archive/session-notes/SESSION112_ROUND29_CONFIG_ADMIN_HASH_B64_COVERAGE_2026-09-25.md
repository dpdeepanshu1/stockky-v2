# Session 112, Round 29 — position-stocks-service `config.py` 471-475 (ADMIN_PASSWORD_HASH_B64 decode)

Closes the last item on the round-24 source-file list: `config.py`
471-475, deferred since round 23 as an `importlib.reload` risk.

## What was covered

- **`config.py` 471-475**: the `ADMIN_PASSWORD_HASH_B64` → `ADMIN_PASSWORD_HASH`
  decode block that runs once at module import time, off whatever env vars
  are set at that moment — not wrapped in any function, so the only way to
  exercise both branches (decode succeeds / decode raises) is to set the
  env vars and `importlib.reload(config)` so the module body runs again.
  real-trade-service already solved this exact problem for the
  byte-for-byte identical block in its own `config.py` (`tests/test_config.py`,
  its round covering that service's lines 84-88) — ported that fixture and
  its four tests into this service's `tests/test_config_getters.py`
  (b64-decoded-when-set, invalid-b64-falls-back-to-empty,
  plain-hash-takes-priority-over-b64, neither-set-gives-empty). The
  `_restore_config_env` fixture snapshots both env vars before each test
  and reloads `config` back to its original state on teardown, so no
  cross-test pollution for every other file in the suite that imports the
  same module object.
- Unlike every round since 23, this one **is pure stdlib** — `config.py`
  only imports `os`, so no `sqlalchemy`/`websockets`/`httpx` stub was
  needed. All four tests were run for real in this sandbox against the
  actual unmodified module (not a hand-reimplementation) — see the round's
  transcript: `test1 OK` / `test2 OK` / `test3 OK` / `test4 OK`.

## Not done this round

- This closes every item on the round-24 source-file list. What's left in
  the fresh coverage paste is entirely inside `tests/*.py` files' own
  coverage (`test_db.py`, `test_dhan_client.py`, `test_edis_precheck.py`,
  `test_entry.py`, `test_eod_squareoff.py`, `test_main.py`,
  `test_oracle_compat.py`, `test_screening_support.py`,
  `test_scrip_master.py`, `test_tz_utils.py`, `test_ws_client*.py`) —
  flagged in round 28, still unaddressed, still out of scope unless the
  plan is explicitly extended to test-file self-coverage.
- PB FinTech `legDetails` root-cause thread still untouched.
