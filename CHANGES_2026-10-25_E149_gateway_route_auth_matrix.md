# E149 - gateway route-auth matrix + three open routes closed (2026-10-25)

Includes E148 / E148b. Plan item: "Security - admin guard coverage, route auth matrix" (service sheet check 6, api-gateway).
Full complete / remaining list: `docs/AUDIT_STATUS_E149.md`. Matrix and decisions: `docs/ROUTE_AUTH_MATRIX_E149.md`.
Roll back: restore the files named under each heading from the E148b zip; each change is small and separate.

## Behaviour change you will notice
Three gateway routes now need the admin login (the same token Auto Trade / Position Stocks issue). **No screen, script, workflow or other service calls any of them** (searched frontend, scripts, .github, deploy, all services), so nothing in the app changes.
| Route | What it did while open |
| --- | --- |
| `POST /ops/qstash/publish` | Relayed a body to ANY url through your QStash token (open relay on your Upstash account). |
| `POST /api/feed/purge-over-cap`, `POST /data-feed/purge-over-cap` | Deleted every feed row priced above `MAX_UNIVERSE_PRICE`. |
A caller with no token now gets 401; a gateway with no session secret answers 503 (fails closed, as for the other guarded routes). `GATEWAY_ADMIN_GUARD=false` still turns the guard off.
If you call one of these from a cron or `curl` that is not in the repo, add `-H "Authorization: Bearer <admin token>"`.

## Files
- `services/api-gateway/admin_guard.py` - three paths added to `PROTECTED`.
- `services/api-gateway/gateway_support/bound_routes.py`, `bound_routes_f.py` - `dependencies=[ref.Depends(ref._require_admin)]` on the three decorators (the existing sweep test in `tests/test_admin_guard.py` keeps `PROTECTED` and the decorators identical, and checks each is guarded on the live app).
- `frontend/src/gatewayAuth.ts` + `gatewayAuth.test.mjs` - the browser helper's path list mirrors the new paths (so a future button on them sends the saved token); `/ops/qstash/tick` and a look-alike `/ops/qstash/publisher` are tested to stay token-free.

## New read-only tooling
- `scripts/audit_route_auth.py` - every mutating gateway route (109 distinct path + verb): guarded or open, and who calls it (frontend / scripts and workflows / other services). `--all`, `--json`, and `--check` (exit 1 on an unguarded route that is not triaged, or a stale triage entry).
- `docs/ROUTE_AUTH_TRIAGE.json` - one entry per open route: class (`ui-open` 59, `uncalled` 20, `machine` 9, `machine-signed` 1), what it can do. A new open mutating route fails `--check` until it is triaged or guarded.
- `scripts/test_audit_route_auth.py` - the script sees the earlier guards and the router-level guard, the three E149 routes stay guarded, and every open route is triaged.

## Tests
Offline here: `scripts/test_audit_route_auth.py` (5 pass), `gatewayAuth.test.mjs` (pass), the `PROTECTED` vs decorator sweep re-computed by hand (identical), compile of `api-gateway` and `scripts`, `check_shared_copies.py` (no new drift), `check_secrets.py`.
NOT run here (no `fastapi`, `pytest`, `sqlalchemy`, registry): `services/api-gateway/tests/test_admin_guard.py` - run it on the VM.
