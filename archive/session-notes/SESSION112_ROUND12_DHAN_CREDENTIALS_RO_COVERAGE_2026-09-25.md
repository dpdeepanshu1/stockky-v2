# Session 112, round 12 (2026-09-25): `auth/dhan_credentials_ro.py` coverage 46% → ~100% (position-stocks-service)

Next item by priority off round 11's list (`auth/dhan_credentials_ro.py`
46%, 32 missing lines: 62-67, 74-87, 92-93, 101-106, 118-138 — essentially
every function). Confirmed via the user's own pytest run this round that
rounds 8-11's work landed correctly: `capital/shared_exposure.py`,
`boot_forensics.py`, `tz_utils.py`, and `auth/admin_auth.py` are all now
100% on the actual VM, 1770 passed, 88% overall.

This module is position-stocks-service's READ-ONLY mirror of
real-trade-service's owning `auth/dhan_credentials.py` — it can decrypt
and read the shared Dhan token but must never write to
`trade_credentials` (ownership stays exclusively with real-trade-service
to avoid two services racing to refresh the same 24h token). It has no
twin test file anywhere in the repo (unlike rounds 9-11's ports), so this
round's tests are written fresh, though the `_effective_expiry` /
`get_decrypted_credentials` / `connection_status` test shapes are modeled
on the overlapping sections of real-trade-service's much larger
`tests/test_dhan_credentials.py` (adapted, not ported line-for-line, since
this module's surface doesn't include save/refresh/gate logic and adds its
own `is_connected()` cheap-check the owning module doesn't have).

## What was added

`tests/test_dhan_credentials_ro.py` — real in-memory SQLite using this
module's OWN `_ROBase` metadata (never `models.Base`, matching the
module's explicit "never create_all() the owning service's table"
contract) and real Fernet encryption; only `datetime.now` is frozen
(monkeypatching the module's own `datetime` name, the same technique used
in real-trade-service's dhan_credentials tests).

Covers: `_fernet` (unset/`None` key raises `RuntimeError`, configured key
round-trips), `_effective_expiry` (no expiry, missing `issued_at`,
30-day-stale row clamped to the 24h hard cap, a shorter real expiry kept,
naive DB datetimes treated as UTC), `get_decrypted_credentials` (no row,
empty token, round trip, rotated key fails closed with an error log that
never contains the token, corrupted blob fails closed, unset key raises
rather than returning `None` — the same "loud on misconfig, quiet on
wrong key" contract as the owning module), `is_connected` (no row, `None`
token, empty token, real token, and confirming it never calls `_fernet()`
— works even with no key configured and a blob that wouldn't decrypt),
and `connection_status` (exact not-connected shape, connected shape +
countdown numbers, countdown moving with a re-frozen clock, the 30-day
stale-row hard-cap display, expired token reporting invalid + zero
seconds, no-expiry row, missing-`issued_at` row, and confirming the
payload JSON never contains the token, client id, or the word
"encrypted").

No bug found this round — pure coverage gap, same class as rounds 9-10.

## Verification

`sqlalchemy` isn't installed in this sandbox (no network), so the actual
pytest file wasn't run here. Since the functions under test only ever call
`db.query(TradeCredentialRO).first()`/`.one()` and touch attributes on the
returned row, hand-verified by stubbing a minimal `sqlalchemy`/
`sqlalchemy.orm` (just enough for the module to import) and a bare
`config`, then importing the real, unmodified `auth/dhan_credentials_ro.py`
and driving it with a tiny fake `FakeDB`/`FakeQuery` plus plain
`SimpleNamespace` rows in place of the ORM model — real Fernet encryption
throughout, not mocked. Ran 48 checks covering every scenario listed above
directly against the real module; 48/48 passed. `py_compile` clean on the
test file.

## Next by priority (unchanged from round 11's list, minus this item)

`pipeline_status.py` 31%, then `db.py` 16%, `execution/dhan_client.py`
21%, `feed/*` and `main.py`.
