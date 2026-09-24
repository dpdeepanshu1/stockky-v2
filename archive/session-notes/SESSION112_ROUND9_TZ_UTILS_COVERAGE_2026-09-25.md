# Session 112, round 9 (2026-09-25): `tz_utils.py` coverage 71% → 100% (position-stocks-service)

Next item by priority off round 7/8's list (`tz_utils.py` 71%, 12 missing:
87-88, 99-104, 134-135, 142, 147 — `is_nse_holiday`'s body,
`is_market_open_ist`'s weekend/holiday/hours branches, `parse_hhmm`'s
`except` fallback, `ist_time_at_or_after`, `is_ist_weekday`). Same root
cause as every prior "0% direct coverage on a pure helper" module in this
plan (session93's `intraday_eligibility.py`, session94's
`event_depth_local.py`, session106's `tz_utils.py` in real-trade-service):
every call site in this service monkeypatches these functions away instead
of exercising the real ones.

## What was added

`tests/test_tz_utils.py` (29 tests) — **ported, not rewritten**, from
real-trade-service's `tests/test_tz_utils.py` (session106). Diffed the two
services' `tz_utils.py` first: they differ only in docstring wording (the
AUDIT FIX narrative names different call sites per service) — every
function body, including the holiday set and market-hours boundaries, is
byte-identical. So the same test bodies apply unchanged; only the module
docstring was updated to describe this file's own coverage numbers and
origin.

Covers: `is_nse_holiday` (holiday / ordinary day / default-now branches),
`is_market_open_ist` (weekday+hours true, Saturday, Sunday, holiday-during-
hours — the exact 2026-09-14 incident the module's own AUDIT FIX comment
describes, before-open, after-close, both boundary times), `as_aware`
(None / naive / already-aware), `ist_now`/`ist_today_str` (UTC→IST offset,
date-rollover formatting), `parse_hhmm` (valid, garbage, `None`, malformed
one-part input — the `except` fallback), `ist_time_at_or_after` (past,
before, exactly-at target), `is_ist_weekday` (weekday, Saturday, Sunday,
and confirming it does NOT consult the holiday list — 2026-09-14 is a
holiday but a Monday, must still return `True`), `iso_utc` (None passthrough,
naive-gets-offset, already-aware preserved).

No bug found — this was a pure coverage gap, same as session106's pass on
the twin file.

## Verification

Pure stdlib (`datetime`, `zoneinfo`), zero DB/network dependency, so —
same as session94/106 — this one could be verified for real rather than
only hand-traced: ran a standalone script importing the actual
`tz_utils.py` and asserting all 29 test bodies directly. All 29/29 passed
against the real module. `py_compile` clean on the test file.
pytest/sqlalchemy still unavailable in this sandbox (no network), so the
actual `pytest tests/test_tz_utils.py` run itself needs confirming on the
VM, but the underlying logic is confirmed correct, not just traced.

## Next by priority (unchanged from round 7/8's list, minus this item)

`boot_forensics.py` 70% (41), `auth/admin_auth.py` 29% and
`auth/dhan_credentials_ro.py` 46%, `pipeline_status.py` 31%, then `db.py`
16%, `execution/dhan_client.py` 21%, `feed/*` and `main.py`.
