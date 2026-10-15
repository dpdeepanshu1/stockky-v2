# Group 403 - engine fingerprint on /health (real-trade, position-stocks, technical)

## Why
Five services carry a vendored copy of the market-structure engine and only the technical service applies `STRUCTURE_PARAMS_JSON`. After a deploy there was
no cheap way to confirm that all of them run the same engine, or which params the technical service really applies (the calibration step ends in
`STRUCTURE_PARAMS_JSON`, so a silent mismatch would make the observations and the gates disagree).

## What
- `shared/market_structure/info.py` (synced to every `stockky_ms/`): `engine_info(params=None)` -> `engine_version`, `schema_version`, `code_hash`, `params_hash`.
  `code_hash` is the first 8 hex of sha1(`MANIFEST.sha256`). The manifest is identical in every in-sync copy, so equal values = same engine code; a stale or
  hand-edited copy differs. Never raises (`{"error": ...}`).
- real-trade `GET /health` and position-stocks `GET /health`: new key `structure_engine` (version, schema, code_hash). No params hash on purpose: these services never override params.
- technical service `GET /health`: `structure.engine` with the same fields plus `params_hash` of the params actually applied (defaults when `STRUCTURE_PARAMS_JSON` is unset or invalid). Existing keys unchanged.

## After deploy
`curl` the three `/health` endpoints and compare `code_hash` (must be equal everywhere) and `engine_version`. A different `params_hash` between technical and an
earlier run means `STRUCTURE_PARAMS_JSON` changed.

## Checked / not checked
Shared engine suite 126 passed; vendored-copy tests ran in both services. The `/health` route tests skip when fastapi is missing, which it was here, so run
`bash run_tests.sh` on the VM. The technical service's `/health` change was only compiled, not run.
