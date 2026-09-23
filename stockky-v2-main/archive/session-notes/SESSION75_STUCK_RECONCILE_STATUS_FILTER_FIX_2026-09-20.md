# Session 75 — stuck-reconcile status-filter gap (2026-09-20)

Found from **your live `/reconcile/pending` output**, not a code read — this is exactly
why that diagnostic exists.

## The bug

You ran `GET /positionstocks/reconcile/pending` and it showed 2 real stuck rows:

| symbol | status | entry | exit | realized_pnl | error_message |
|---|---|---|---|---|---|
| INDORAMA | `STOP_HIT` | 91.88 | 90.04 | -42.32 | `EOD_SQUAREOFF_PENDING_RECONCILE: exit_price=entry_price placeholder...` |
| NAHARINDUS | `STOP_HIT` | 139.5 | 135.4 | -28.70 | same sentinel |

Both already had a **correct, real exit price** (different from entry_price, with a
matching nonzero P&L) — they were fully resolved in substance. But the leftover
`EOD_SQUAREOFF_PENDING_RECONCILE` sentinel text was never cleared, so the diagnostic
kept reporting them as unresolved, age 2-3 days.

Traced it to `orders/reconcile.py::resolve_stuck_pending()` — the function session 72
built specifically to close these out. Its query was:

```python
rows = (db.query(ScalpPosition)
        .filter(ScalpPosition.status.in_(_FLAT_SELL_PENDING_STATUSES),   # <- the bug
                ScalpPosition.error_message.like("%_PENDING_RECONCILE%")).all())
```

`_FLAT_SELL_PENDING_STATUSES = ("EOD_SQUAREOFF", "MANUAL_EXIT", "STAGNATION_EXIT")` —
but both stuck rows had `status="STOP_HIT"`, which isn't in that tuple. So the
resolution/self-heal sweep **never even looked at them** — while `list_pending_reconcile()`
(the `GET /reconcile/pending` diagnostic itself) has always matched on the sentinel text
alone, with no status restriction. The diagnostic and the resolver disagreed on what
counts as "stuck," so any row whose status ended up outside those 3 values (a real
exit that raced with — or followed — an earlier EOD-flatten attempt) was invisible to
self-heal, trade-history resolution, and the 3-day age-out alert alike. It would have
sat "pending" **forever**, silently, with no alert ever firing, since the age-out path
that's supposed to catch exactly this was equally gated by the same filter.

## The fix

Dropped the `status.in_(...)` filter from `resolve_stuck_pending()`'s query — it now
matches `list_pending_reconcile()` exactly (sentinel text only). None of the function's
internal logic (self-heal check, trade-history match, age-out rewrite) actually
depends on `status`, so this only widens *which rows get examined*, not what happens
to them once found.

`services/position-stocks-service/orders/reconcile.py` — that's the only file changed.

## Verified

New test `tests/test_stuck_pending_status_gap.py` (4 tests) recreates the exact live
shape — a `STOP_HIT` row and a `TARGET_HIT` row, both with an already-correct
exit_price/realized_pnl and a leftover sentinel — and confirms both are now
self-healed. Also confirms the previously-working `EOD_SQUAREOFF` case still passes,
and that a normal `OPEN` position with no sentinel is still completely untouched (the
fix widens status coverage, not sentinel matching). Full suite:
`cd services/position-stocks-service && python -m pytest tests -q` — 41 passed.

## Live check

Your two stuck rows (INDORAMA id=13, NAHARINDUS id=11) will self-heal on this
service's next reconcile sweep after redeploy (throttled to
`PENDING_RECONCILE_SWEEP_INTERVAL_S`, default 600s) — or immediately via
`POST /positionstocks/reconcile/pending/resolve` with a valid admin bearer token
(get one from `POST /positionstocks/auth/login`, not `/realtrade/...` — that's what
gave you "Invalid or expired admin session" last time, an empty/wrong `$TOKEN`, not a
bug). After either, `GET /positionstocks/reconcile/pending` should show `count: 0`.

## Still open

Everything from session 74's list stands unchanged — this was found live, not by
further reading. Same 3 non-code items (`MIN_FUNDAMENTAL_SCORE` floor, Oracle DB
session-cap, and session 73's 3 fixes still awaiting their own live confirmation).
