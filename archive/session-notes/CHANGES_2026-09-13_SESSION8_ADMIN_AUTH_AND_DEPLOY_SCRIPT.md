# Session 8 (2026-09-13) — position-stocks-service admin auth + the actual deploy fix

## 1. Root cause of "nothing works" (nginx) — finally fixed at the deploy layer, not the code layer
Sessions 5-7 all correctly diagnosed and fixed `deploy/nginx-stockky.conf` (added the
missing `/positionstocks/` location block), but the fix was never applied to the
live host. `docker compose up` only manages containers — nothing in this repo
ever copied that file to `/etc/nginx/sites-available/stockky`, symlinked it into
`sites-enabled/`, ran `nginx -t`, or reloaded nginx. That's exactly why the two
screenshots this session showed: GET requests fell through to the frontend's
static-file block (served `index.html`, hence "200 OK: <!DOCTYPE html>"), and
POSTs hit the frontend's GET/HEAD-only handler (hence "405 Not Allowed").

Added **`deploy/deploy.sh`** — the one command to run on the VM after every
`git pull`. It installs the nginx config, tests it, reloads nginx, rebuilds/
restarts containers, and runs a smoke test against the live domain so a future
session (or you) can immediately tell whether the routing is actually live
instead of assuming it from a `docker compose up` that succeeded. This has to
be run manually on the Oracle VM at least once — no sandbox/CI step can do it.

## 2. Admin auth added to position-stocks-service (same password as real-trade-service)
Every mutating route (`/arm`, `/disarm`, `/service/enable`, `/service/disable`,
`/autopilot/enable`, `/autopilot/disable`, `/cycle/run`, `/kill`,
`/ledger/sync`, `/reconcile`) now requires `Authorization: Bearer <token>`,
verified against a JWT issued by a new `POST /auth/login`. This deliberately
duplicates real-trade-service's `auth/admin_auth.py` (Argon2id hash + signed
JWT) rather than importing it, matching this service's existing isolation
design — but it reads the exact same `ADMIN_USERNAME` / `ADMIN_PASSWORD_HASH`
(or `_B64`) / `SESSION_SECRET` env vars already set in `.env` for
real-trade-service, so the same admin password logs into both dashboards.

Read-only routes (`/status`, `/positions`, `/trades/history`, `/candidates`,
`/ledger`, `/ws-status`, `/dhan/live-orders`, `/health`) are unchanged —
still public, same as real-trade-service's own gate-status reads.

Files touched:
- `config.py` — added the admin-auth env vars (identical block to
  real-trade-service's, same B64-escaping rationale for `.env` `$` chars).
- `auth/admin_auth.py` (new) — `verify_admin_password`, `issue_session_token`,
  `decode_session_token`, `require_admin` FastAPI dependency. No
  `require_admin_if_real` equivalent — this service is REAL-only, no DEMO
  mode, so every mutating route just uses plain `require_admin`.
- `main.py` — `POST /auth/login`, `POST /auth/logout` (stateless — no
  server-side session flag persisted the way real-trade-service's
  `gate.admin_authenticated` does; token validity alone gates every call),
  `require_admin` added to the 10 mutating routes listed above, startup
  warning if `ADMIN_PASSWORD_HASH`/`SESSION_SECRET` aren't set.
- `requirements.txt` — added `argon2-cffi==23.1.0`, `pyjwt==2.8.0` (same
  pinned versions real-trade-service uses).
- `docker-compose.yml` — `position-stocks-service` block now explicitly lists
  `ADMIN_USERNAME` / `ADMIN_PASSWORD_HASH` / `ADMIN_PASSWORD_HASH_B64` /
  `SESSION_SECRET` (previously implicit via `env_file`, now explicit to match
  real-trade-service's block and make the shared-password intent obvious).
- `frontend/src/positionStocksApi.ts` — session-token storage (separate
  localStorage key from real-trade-service's, per-service trust boundary),
  `login()`/`logout()`, `Authorization` header auto-attached to the 10
  mutating calls, a clear thrown error ("Admin login required...") if a
  mutating call is attempted with no token instead of a bare 401.
- `frontend/src/components/PositionStocksTab.tsx` — login form (same
  username/password fields, same visual style as `RealAutoTrade.tsx`'s),
  session-expiry hook wired in, every action button's `disabled` now also
  checks `!loggedIn`.

## Verification done in sandbox
- `py_compile` clean on `main.py`, `config.py`, `auth/admin_auth.py`.
- Manual bracket/paren balance check clean on both edited `.tsx`/`.ts` files
  (no local `node_modules` available in the sandbox to run `tsc` directly —
  run `npm run build` on the VM as the real check before/with `deploy.sh`).
- `bash -n deploy/deploy.sh` clean.
- NOT verified: an actual live boot with real admin login end-to-end — that
  needs the VM, and specifically needs `deploy/deploy.sh` run first so the
  request even reaches the container instead of nginx's fallback.
