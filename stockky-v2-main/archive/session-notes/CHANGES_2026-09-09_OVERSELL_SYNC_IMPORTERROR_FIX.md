# 2026-09-09 — Oversell qty-sync silently never ran (ImportError)

## Symptom
ANDHRAPAP kept rejecting with `RMS:...:You are trying to sell more than
the quantity you currently hold`, cycle after cycle, with Stockky's
`qty_open` never correcting itself — even after the 2026-09-09
oversell-detection fix (`is_oversell_error` + the holdings-sync branch in
`exit_engine/exit.py`) was deployed.

## Root cause
The oversell branch added earlier the same day called:

```python
from execution.dhan_client import get_dhan_client
dhan = get_dhan_client(db)
holdings_resp = dhan.get_holdings()
```

`get_dhan_client` does not exist anywhere in `execution/dhan_client.py`
— that module only has a private `_get_sdk_client(db)` helper and a
module-level `get_holdings(db)` function (same pattern as
`get_positions(db)`, `get_order_list(db)`, etc. — all take `db`
directly, none are methods on a client object). The import raised
`ImportError` every single time this branch ran, which was caught by
the surrounding `except Exception as sync_e:` and logged as "holdings
sync failed" — silently, with no alert. `qty_open` was therefore never
capped or ghost-closed, so the exact same stale-qty SELL was retried
next cycle, rejected the same way, forever.

## Fix
1. **`exit_engine/exit.py`** — replaced the broken import/call with
   `dhan_client.get_holdings(db)`, using the module already imported at
   the top of the file (`from execution import dhan_client`) — same
   call pattern as the adjacent `dhan_client.is_oversell_error(...)`.
   The module function returns the holdings list directly, so the old
   `.get("data")` unwrap is also removed (that was matched to a
   different response shape than what this function actually returns).
2. **`exit_engine/exit.py`** — the `except Exception as sync_e:` branch
   had no alert path at all, so a *future* sync failure (holdings API
   down, bad token, etc.) would go just as unnoticed as this one did.
   Added a throttled Telegram alert (same cooldown idiom as the CDSL/
   insufficient-funds branches above it) so any recurrence surfaces
   within one cooldown window instead of failing silently.

## Files changed
- `services/real-trade-service/exit_engine/exit.py`

## Verification
`python3 -m py_compile` clean on `exit_engine/exit.py` and every other
`.py` file in the repo. Confirmed via `grep` that `get_dhan_client` no
longer appears anywhere as a live call (only in the explanatory comment
above), and that `dhan_client.get_holdings` matches the field names
(`tradingSymbol`, `totalQty`) already used by `portfolio.py`'s
`import_broker_holdings()` / `reconcile_broker_removed()` for the same
Dhan holdings response.

## Note on the ⚡ Reset Failures button
The button, its `handleReset()` wiring in `ServiceManager.tsx`, and
both backend endpoints it calls (`api-gateway`'s `POST
/ops/circuit-reset` and `real-trade-service`'s `POST
/resilience/reset`) are all present and correct in this codebase — this
was checked line-by-line this session, nothing was missing. It not
showing up in the screenshot means the previous zip's deploy step
(`git push` + `docker compose build --no-cache frontend` + `docker
compose up -d`) hadn't actually been run yet when that screenshot was
taken, not a code problem. It'll appear next to Refresh / Wake All
after that build runs.
