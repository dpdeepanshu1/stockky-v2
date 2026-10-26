# E170 - service sheets 2 to 12, env reference completed

Includes E169.
- Added `scripts/audit_service_sheet.py` (read-only; one area or `--all`; `--json`): the ten measured checks for every service, `shared/` and `scripts/`.
- Added `docs/SERVICE_SHEET_<area>.md` for all twelve areas (the real-trade sheet from E169 is rewritten; its E170 note lists three corrected numbers: 23 + 10 guarded routes not 31 + 11, 97 route decorators not 95, 92 silent handlers in the service of which 28 in order paths, not 29 in total) and `docs/SERVICE_SHEETS_INDEX.md` with the findings F-24 to F-32.
- `scripts/gen_env_reference.py` now also reads `__import__("os").getenv`, `ref.os.getenv` and wrapper calls such as `_env_int("X", 5)` / `_bs_bool("X", "true")`; `docs/ENV_REFERENCE.md` regenerated: 802 -> 1,492 variables (wrapper-only names marked `(helper)`).
- Two tests added to `scripts/test_repo_scripts.py` (wrapper reads; every area has a tool result and a sheet).
- No service or frontend source changed. Not run: `pytest` (the new tests and the 11 existing ones in that file passed under a stand-in runner, 13 of 13).
Rollback: delete the 13 new docs and `audit_service_sheet.py`, restore `gen_env_reference.py` and `ENV_REFERENCE.md` from E169, drop the two tests.
