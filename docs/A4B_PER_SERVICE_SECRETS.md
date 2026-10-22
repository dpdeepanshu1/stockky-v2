# A4b - one session secret per service (2026-10-22)

## Problem
All four services (real-trade, position-stocks, api-gateway, calibration-runner) signed and verified admin JWTs with the same `SESSION_SECRET`.
A leak from any one of them (the gateway is internet-facing, the runner executes job scripts) could forge an admin session on every service,
including the real-money one. One value also meant one rotation touched everything at once.

## What changed
`session_keys.py` (identical copy in all four services, enforced by `scripts/check_shared_copies.py`).

| Service | Signs with | Accepts (kid) |
|---|---|---|
| real-trade-service | `SESSION_SECRET_REAL_TRADE` | real-trade, position-stocks, calibration-runner |
| position-stocks-service | `SESSION_SECRET_POSITION_STOCKS` | real-trade, position-stocks, calibration-runner |
| calibration-runner-service | `SESSION_SECRET_CALIBRATION_RUNNER` (job token only) | real-trade, position-stocks |
| api-gateway | nothing (verify only) | real-trade, position-stocks |

* Every new token carries a `kid` header naming the signing service. The `kid` picks the key; a wrong key for its kid, an unknown kid, a kid this service
  does not accept, or a kid whose secret is not set here is refused. Nothing falls back to another key.
* The accept list is fixed in code, and compose blanks `SESSION_SECRET_CALIBRATION_RUNNER` on the gateway, so a secret that reaches a container through
  `env_file: .env` is still not trusted there.
* **Backward compatible.** With only the old `SESSION_SECRET` set, everything behaves as before (tokens have no `kid`, the legacy secret signs and verifies).
  A token with no `kid` is checked against `SESSION_SECRET` only. Delete `SESSION_SECRET` and legacy tokens stop working.
* Boot logging: `AUTH CONFIG` now says `legacy-shared` (warning) / `per-service` / `none` (error), flags secrets under 32 characters and two services using
  the same value. The existing `/auth/config-check` diagnostics (real-trade / position-stocks) gain a `session_keys` block with fingerprints only.
* `scripts/generate_secrets.py` prints the three new values. `.env.oracle.example` documents them. `docs/ENV_REFERENCE.md` regenerated.

## Switch-over (about two minutes, one restart)
1. Generate three different values (`python3 -c "import secrets; print(secrets.token_hex(32))"` three times, or `python3 scripts/generate_secrets.py`).
2. Put them in `.env` as `SESSION_SECRET_REAL_TRADE`, `SESSION_SECRET_POSITION_STOCKS`, `SESSION_SECRET_CALIBRATION_RUNNER`. Keep the old `SESSION_SECRET` for now.
3. `docker compose up -d` (recreates all four). Existing logins keep working through the legacy secret; new logins are per-service.
4. Check the boot log line `AUTH CONFIG [...]`: it should say per-service, with no weak / duplicate warning.
5. Remove `SESSION_SECRET` from `.env`, `docker compose up -d` again, log in again. Old shared-secret tokens (including any that were ever printed in a log) are now dead.

Rotate one service later by changing only its own value; tokens from that service stop working, the others are unaffected.

## Limit (be honest about it)
HS256 is symmetric: a service that can verify a kid's tokens could also forge them. This cuts the number of places that hold each key (the gateway holds no
runner key, the runner holds no gateway-only secret because there is none) and gives per-issuer rotation and attribution, but it does not give "verify without
being able to sign". That needs an asymmetric algorithm (EdDSA / RS256) and would be its own change.

## Tests
`services/real-trade-service/tests/test_a4b_session_keys.py` (12), `services/api-gateway/tests/test_admin_guard.py` (3 new). Existing tests use the legacy secret and
are unchanged.
