# Session 53 (2026-09-16) — Ledger sync never actually updated: narrow balance-key list

## Symptom

After session 52's Option A + capital-erosion fixes were delivered, the user
ran the requested diagnostic commands live:

- `GET /status`: everything healthy — `armed`, `service_enabled`,
  `auto_pilot_enabled`, `market_open` all `true`; no kill switch; circuit
  breaker `closed`; ws `connected`; `last_cycle_run_at` fresh (`09:59:26`,
  `AUTO` trigger).
- `GET /ledger`: `total_allocated_capital`/`available_capital` still `52.45`,
  `last_synced_from_broker_at` still `08:29:26` — over an hour stale.
- `GET /candidates/log?limit=15`: fresh `SKIPPED:INSUFFICIENT_CAPITAL`
  entries at `08:59` (and earlier), proving cycles were reaching
  `orders/entry.py`'s `ledger.reserve_capital()` call — i.e. past the
  `ledger.sync_from_broker()` call that happens earlier in the same cycle.
- A manual `POST /cycle/run` at `10:00:33` UTC (15:30 IST) correctly returned
  `skipped_reason: "PAST_EOD_TIME"` — expected, not a bug, since that request
  landed after the service's hard 3:00 PM IST flat-by policy.
- `GET /status/REAL` (real-trade-service, same moment) showed
  `cash_available: 4482.3` — a live, successfully-resolved balance.

The combination of "cycles running and reaching the capital check" +
"sync timestamp never moving" only makes sense if `sync_from_broker()` was
being called every cycle but silently failing to update anything.

## Root cause

`capital/ledger.py`'s `sync_from_broker()`:

```python
available_balance = float(funds.get("availabelBalance") or funds.get("availableBalance") or 0.0)
if available_balance <= 0:
    logger.warning(...)
    return 0.0
```

only checks 2 of Dhan's known available-balance field names. Meanwhile
`real-trade-service/execution/equity_sync.py` checks 5:

```python
_BALANCE_KEYS = (
    "availabelBalance", "availableBalance", "availableCash",
    "withdrawableBalance", "sodLimit",
)
```

and, in the same diagnostic run, real-trade-service successfully resolved a
live balance. Since both services call the same underlying Dhan
`get_fund_limits()` SDK call on the same account, the only explanation is
that Dhan is currently populating this account's funds response under a key
outside position-stocks-service's narrower 2-key list (most likely
`availableCash`, given equity_sync.py's own key-order) — so
`available_balance` silently evaluated to `0`, and `sync_from_broker()`
returned early without ever touching `total_allocated_capital` or
`last_synced_from_broker_at`. This has been happening on every cycle all
day, independently of and on top of the two capital-split bugs already fixed
in session 52 — even with those fixed, the pool could never have grown,
because it was never actually re-reading Dhan's balance at all.

## Fix

`capital/ledger.py` now mirrors `equity_sync.py`'s exact balance-key
fallback logic: the same `_BALANCE_KEYS` tuple (same order) and a
`_pick_balance()` helper, plus the same "log once when a balance field is
first used, and again if the matched key ever changes" tracking, so a future
Dhan response-shape shift shows up in the logs instead of silently starving
the pool the way this one did all day.

`py_compile` + `pyflakes` clean on `services/position-stocks-service/capital/ledger.py`.

## What to check after deploy

```bash
curl -s http://localhost:8006/ledger | python3 -m json.tool
```
`last_synced_from_broker_at` should now be within the last ~10s during market
hours, and `total_allocated_capital` should jump to a real fraction of Dhan's
actual free cash (expect roughly 50% of whatever real-trade-service's own
`broker_cash_available` is now showing, once real-trade-service's positions
have had a chance to close down under its own new cap).

Check the service logs for a line like:
```
ledger.sync_from_broker: using Dhan balance field '<key>' for the scalp pool — verify this reflects a sensible current tradeable balance.
```
to confirm which field actually matched, and sanity-check that figure against
Dhan's app.
