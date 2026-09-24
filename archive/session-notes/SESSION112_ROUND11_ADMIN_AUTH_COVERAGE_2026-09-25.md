# Session 112, round 11 (2026-09-25): `auth/admin_auth.py` coverage 29% → ~100% (position-stocks-service)

Next item by priority off round 10's list (`auth/admin_auth.py` 29%, 47
missing lines: 45-56, 60-66, 72-80, 88-94, 103-106, 118-125 — essentially
every function body except the module-level constants). This is the
Layer-1 gate in front of every mutating route in this service (real
money), so it's worth exhaustive branch coverage, not just line coverage.

This service already had `tests/test_admin_auth_e2e.py` (subprocess-level,
proves login + a route + cross-service token acceptance both ways), but
nothing exercising `auth/admin_auth.py`'s own branches directly — which is
exactly why the module itself was still at 29%.

## What was added

`tests/test_admin_auth.py` — ported from real-trade-service's
`tests/test_admin_auth.py` after diffing the two `auth/admin_auth.py`
files: identical apart from docstrings and one function real-trade-service
has that this service doesn't (`require_admin_if_real` — this service has
no DEMO/REAL split, it's real-money-only, so every mutating route uses
plain `require_admin`). Ported everything else unchanged, dropped the
`require_admin_if_real` import and its `TestRequireAdminIfReal` class, and
updated the module docstring's coverage numbers/line list to this
service's own baseline.

Covers: `verify_admin_password` (no hash configured, wrong username never
touches the hasher, case-sensitive username/password, correct/wrong
credentials, malformed stored hash, unexpected verifier exception fails
closed + logs a warning + never logs the plaintext password),
`issue_session_token` (no secret, happy path claims/expiry, configurable
idle timeout, cross-secret unverifiable), `decode_session_token` (round
trip, no secret, expired, garbage, wrong secret, tampered payload, `alg:
none` downgrade attack, non-allow-listed HMAC algorithm, missing `sub`
claim), `require_admin` (missing header, non-Bearer scheme, garbage token,
empty token, expired token, valid token, whitespace-tolerant, secret
rotation invalidates existing sessions), and
`auth_config_diagnostics`/`log_auth_config` (fully-configured snapshot,
secret/hash never appear in the snapshot or logs, missing-secret and
missing-hash and non-argon2-hash each produce exactly the right ERROR
line, all-good path logs INFO only).

Real Argon2id + real PyJWT in the source file — no mocking of the crypto
in the ported suite itself, so a library upgrade that changes exception
behaviour would fail there, not in production.

No bug found this round — pure coverage gap.

## Verification

`argon2` and `fastapi` aren't installed in this sandbox (no network), so
the ported pytest suite itself wasn't run here — same constraint as prior
rounds. Verified the underlying logic directly instead: stubbed minimal
`fastapi` (`HTTPException`, `Header`) and `argon2` (a hash-compatible fake
hasher raising the same `VerifyMismatchError`/`InvalidHashError` on the
same conditions as the real library) and a bare `config` module, then
imported the real, unmodified `auth/admin_auth.py` and ran 32 checks
against it directly — every function, every branch listed above,
including the JWT downgrade/tampering/wrong-algorithm rejections (using
the real `jwt` library, which is installed). 32/32 passed. `py_compile`
clean on the ported test file. The Argon2id crypto itself wasn't
exercised for real (stub only reproduces the control flow, not the actual
hashing), so the crypto-specific assertions in the ported suite (hash
format, timing behaviour) still need a real pytest run on the VM to fully
confirm — flagging that distinction explicitly, unlike the fully-stdlib
modules from rounds 9-10.

## Next by priority (unchanged from round 10's list, minus this item)

`auth/dhan_credentials_ro.py` 46%, `pipeline_status.py` 31%, then `db.py`
16%, `execution/dhan_client.py` 21%, `feed/*` and `main.py`.
