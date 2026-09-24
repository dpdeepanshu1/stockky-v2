# Session 96 (2026-09-24): auth/dhan_credentials.py — 18% → 100%, plus two production fixes

## What was already in this zip coming in

The pasted VM transcript confirmed session95 on the live box: **1816 passed,
1 xfailed, 93% total, `resilience/local_cache.py` 100%.** Nothing failing.
Next item off the session95 priority list: `auth/dhan_credentials.py`
(194 stmts, 18%, 159 missed) — the module that decides whether REAL trading may
touch the Dhan account at all (Fernet-at-rest storage, the local-clock expiry
math the gate state machine polls, live-token / invalid-IP auto-disarm, and the
opt-in TOTP auto-refresh that talks to auth.dhan.co with the account PIN).
Everything else in the suite mocked this module at its own boundary.

## Two production bugs found while writing the tests (both fixed)

### 1. The Dhan PIN, client id and one-time TOTP code leaked into the log and into Telegram

`refresh_if_totp_enabled()` calls `httpx.post("https://auth.dhan.co/app/generateAccessToken",
params={"dhanClientId": …, "pin": …, "totp": …})` — Dhan takes all three as
**URL query parameters**. `httpx`'s `HTTPStatusError` message embeds the full
request URL. Verified directly on the pinned `httpx==0.25.2` (and 0.28.1):

    Client error '401 Unauthorized' for url
    'https://auth.dhan.co/app/generateAccessToken?dhanClientId=1100123456&pin=987654&totp=123456'

The `except` block did `logger.error("… failed: %s", e)` **and**
`notify_sync(f"🚨 *Dhan TOTP refresh FAILED*\n{str(e)[:300]}")` — so *any* 4xx/5xx
from that endpoint (the most likely failure there is: a wrong/expired TOTP or a
changed PIN) wrote the account PIN to the container log and sent it, in the
first 300 chars, to the Telegram chat. The PIN is static; together with
`DHAN_TOTP_SECRET` it is what mints access tokens.

**Fix:** new `_redact_secrets(text, *secrets)` — (a) replaces the value of any
`dhanClientId` / `pin` / `totp` / `access_token` query parameter with `***`,
(b) replaces every literal secret (≥4 chars) as defence in depth for exception
types that echo an input without a `key=` prefix. It runs **before** the
300-char cut (cutting first could slice `pin=987654` into `pin=98` and dodge
the pattern). The alert still says `401 Unauthorized` and the endpoint, just
with `dhanClientId=***&pin=***&totp=***`.

**Action for you (not something code can do):** if TOTP refresh has ever failed
on the live box with `DHAN_TOTP_ENABLED=true`, the PIN and client id are
sitting in `docker compose logs real-trade-service` history and in your
Telegram chat history. Check with
`docker compose logs real-trade-service 2>&1 | grep -c "pin="` — if non-zero,
consider changing the Dhan PIN and deleting those Telegram messages.
(If you run in manual-paste mode, `DHAN_TOTP_ENABLED=false`, this never fired.)

### 2. A swallowed DB failure left the caller's Session poisoned

`refresh_if_totp_enabled()` is documented non-fatal and runs on the *caller's*
Session (`cycle_runner` reuses that same Session on the very next line for
`enforce_live_token`). If the DB failed while saving the new token — or while
restoring `gate.dhan_connected` — the exception was caught and logged but the
Session was left in the partial-rollback state, so the next query in the cycle
raised `PendingRollbackError`. Reproduced for real in the tests with an
SQLAlchemy hook that fails the write.

**Fix:** new `_heal_session(db)` rolls back **only if `db.is_active` is False**
(i.e. only a poisoned Session), called from both failure paths. A healthy
caller Session (e.g. the HTTP call failed before touching the DB) is left
alone so its pending state is never discarded — pinned by a test.

## What was added

New `tests/test_dhan_credentials.py` — **156 tests** against a REAL in-memory
SQLite DB, REAL Fernet, REAL pyotp. The only fakes: `httpx.post` (returns real
`httpx.Response`/`Request` objects so `raise_for_status()` produces the genuine
error text), `notifier.notify_sync` (captured), `dhan_client.verify_token_live`,
a frozen `dc.datetime.now` where exact arithmetic matters, and a
`before_cursor_execute` hook that makes chosen writes fail.

- **`_effective_expiry` / `is_token_valid` / `token_needs_refresh`** — 24h hard
  cap clamping a stale 30-day row (the CHANGES_2026-08-27 #2 class), naive
  values stamped UTC, exact expiry instant, exact refresh-margin boundary
  (`>=`), and a proof that the hard cap — not the stored expiry — is what
  enforces the 24h.
- **`save_credentials` / `get_decrypted_credentials` / `connection_status`** —
  plaintext never in any column, single-row overwrite, guess-vs-real expiry,
  rotated key and corrupted blob fail closed to `None`, exact not-connected
  shape, countdown numbers, and the status payload never containing a secret.
- **`enforce_live_token`** — every auth marker disarms REAL (9 real Dhan
  strings), transient errors (429, reset, DH-904, empty, `None`) never disarm
  on a guess, no-gate row, per-mode isolation, warning only if it *was* armed,
  reason truncated to fit `String(255)`.
- **`disarm_on_invalid_ip`** — return value = "was armed", reason text,
  `dhan_connected` left alone, per-mode isolation.
- **`_parse_dhan_expiry`** — epoch s/ms, the CONFIRMED-2026-09-03 bare-ISO-is-IST
  format (regression for the +5:30 bug), Z/offset, space-separated, the
  non-zero-padded shape only the `strptime` fallback can read, garbage, blank
  (no spurious error log), NaN/inf/overflow.
- **`refresh_if_totp_enabled`** — disabled = no HTTP even with valid env,
  missing env, exact request (URL, params, 15s timeout, a *real* TOTP code),
  every token-field shape, nested/top-level/unparseable/missing expiry, gate
  healing (REAL only, only when it was False, never re-arms), 401/500,
  network error, bad JSON, wrong-shaped JSON, pyotp missing, bad TOTP secret,
  missing encryption key, DB failure on save and on gate heal, notifier
  failure on both paths, 300-char cap, **and the two regressions above**.

## Verification

- Full `real-trade-service` suite as on the VM: **1972 passed, 1 xfailed**
  (1816 + 156), overall **93% → 94%**, `auth/dhan_credentials.py` **100% (215/215)**.
- **The regression tests fail on the pre-fix module**: run against the original
  file, 24 tests fail — including the end-to-end ones, whose captured log line
  literally shows `…generateAccessToken?dhanClientId=1100123456&pin=987654&totp=806730`.
- **Mutation-checked**: 38 deliberate regressions to `dhan_credentials.py`
  (clamp removed, `<`→`<=`, `>=`→`>`, auth check dropped, IST parsed as UTC,
  ms guard off, TOTP flag check off, PIN param dropped, gate-heal made
  unconditional / pointed at DEMO, real expiry not stored, redaction removed
  or made literal-blind, rollback always/never, both heal calls removed, raw
  exception put back in log and in Telegram, notifications removed, …).
  First run caught 36; the two survivors were **weak tests, not equivalent
  mutants** — the "disabled is a no-op" test had no env set so the
  missing-secret guard masked the missing flag check, and the blank-string
  test didn't assert the absence of a spurious error log. Both tightened.
  Final: **0 survivors**; the module was restored byte-identical each time.

## Observations (not changed)

1. **Missing encryption key ≠ "fail closed".** `get_decrypted_credentials`'s
   docstring says it returns `None` when decryption fails, but only
   `InvalidToken` (rotated key / corrupt blob) is caught; an unset or malformed
   `DHAN_CREDENTIAL_ENC_KEY` raises `RuntimeError`/`ValueError`. In
   `dhan_client.verify_token_live` that lands in the generic `except Exception`
   and `enforce_live_token` then treats it as a *transient* error (token "ok"),
   so the failure surfaces later as an order-placement error instead. Not
   reachable in a correctly-booted service (`config.validate()` rejects a
   missing key), so pinned as-is by a test rather than changed.
2. **A 200 response with no token field logs but does not Telegram.** Every
   other refresh failure sends the 🚨 alert; this one only logs
   "no token field in response". `enforce_live_token` will still disarm and
   alert once the old token actually dies, so it is a delay, not a hole.
3. **`DHAN_PIN` isn't validated.** Only `DHAN_TOTP_SECRET` and `DHAN_CLIENT_ID`
   are guarded; an unset PIN is sent as `pin=` and Dhan rejects it every
   attempt (one 🚨 per attempt). Pinned by a test.
4. **A parsed `expiryTime` in the past is stored as-is** (only *longer* than 24h
   is clamped). A wrong-timezone assumption or garbage epoch would make a
   freshly minted token look expired and auto-disarm REAL. The IST handling was
   confirmed against a real response on 2026-09-03, so this is theoretical.
5. Carried over: `pipeline_status` stage timings misattributed since
   session48b (session94), `json.dumps` outside `save_snapshot`'s `try`
   (session95) — both still open, observability only.
6. Not audited this session: `position-stocks-service`/`market-data-service`
   AngelOne login (`pyotp` usage in `angelone_session.py` / `angelone_client.py`).
   They go through the AngelOne SDK, not a query-string URL, so the same leak
   shape is unlikely — but nobody has checked what their `except` blocks log.

## Still open, in priority order

1. `db.py` (7%, 482 missed) — migrations; the session-11 model-vs-migration bug class
2. `market_feed/feed.py` (66%), `entry_engine/entry.py` (84%)
3. small modules: `notifier.py` 48%, `symbol_master.py` 29%,
   `shared_adaptive.py` 27%, `boot_forensics.py` 18%, `admin_auth.py` 61%,
   `shared_exposure.py` 76%, `event_depth_local.py` 40%, `pipeline_status.py` 91%
4. `offline_test_harness.py` (288 stmts, 0%) is a dev harness — consider
   excluding it from coverage.
