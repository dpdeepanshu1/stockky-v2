# Session 22b — Position Stocks Tab: Order-ID Wiring + Full Audit

**Requested:** "audit position stock tab fully" (continued from session 22 —
kill-switch reset gate-sync fix)

## Bug found and fixed: `dhan_exit_order_id` captured but never surfaced

`orders/reconcile.py::run_exit_reconciliation()` has captured
`dhan_exit_order_id` for every closed scalp position since it was added to
`models.py` — the specific `orderId` of whichever leg (TARGET_LEG or
STOP_LOSS_LEG) actually filled, distinct from the super/bracket order id
already shown everywhere. It was written to the DB on every close, but:

- `GET /positions` returned `dhan_super_order_id` only.
- `GET /trades/history` returned `dhan_super_order_id` only.
- `ScalpPositionRow` / `ScalpTradeRow` (frontend types) didn't declare the
  field, so even a backend fix alone wouldn't have reached the UI.
- `PositionRow` (Overview subtab cards) rendered `Order {super_order_id}`
  only — no exit-leg id anywhere.
- The Trade History table had no order-ID column at all.

Fixed all four layers:
- `main.py`: both `/positions` and `/trades/history` now also return
  `dhan_entry_order_id` and `dhan_exit_order_id`.
- `positionStocksApi.ts`: `ScalpPositionRow` and `ScalpTradeRow` both grew
  the two new nullable fields.
- `PositionStocksTab.tsx`: `PositionRow` now appends `· Exit Order {id}`
  when the exit leg's id is real and differs from the super order id
  (reconcile.py falls back to the super order id itself when Dhan's leg
  payload omits its own `orderId`, in which case there's nothing new to
  show). The Trade History table gained an "Order IDs" column showing
  Entry/Exit ids per row.

## Bonus bug found while wiring this up: `dhan_entry_order_id` was dead

`dhan_entry_order_id` has existed as a column on `ScalpPosition` since
session 19's tag-based-filter fix (it was added alongside
`dhan_super_order_id`/`dhan_exit_order_id` for the same purpose) but
`orders/entry.py::attempt_entry()` never actually wrote to it — only
`dhan_super_order_id` was set on the new `ScalpPosition` row. Exposing it
in the API as-is would have meant a column that's *always* `null`.

Checked Dhan's actual super-order response shape (documented in
`reconcile.py`'s own module docstring): a super order has **one**
top-level `orderId` for the whole bracket, and that top-level row **is**
the `ENTRY_LEG` (`legName="ENTRY_LEG"` on the parent row itself, not a
separate id inside `legDetails`). Dhan doesn't issue a distinct id for
the entry leg — so there was never a real value to capture beyond what
`dhan_super_order_id` already holds.

Fixed by setting `dhan_entry_order_id=dhan_super_order_id` at position
creation in `entry.py`, so the field is meaningful (and named honestly)
instead of permanently `null`. `PositionRow`'s existing `Order {id}`
suffix already covers this value — no separate frontend label needed
for entry specifically, since it's the same id as the bracket order.

## Broader audit of the Position Stocks tab (this pass)

- Cross-checked every route in `main.py` (`/health`, `/auth/*`, `/status`,
  `/arm`, `/disarm`, `/service/*`, `/autopilot/*`, `/cycle/run`, `/kill`,
  `/positions`, `/trades/history`, `/candidates`, `/candidates/log`,
  `/ledger`, `/ledger/sync`, `/ledger/reset-daily`, `/ws-status`,
  `/dhan/account`, `/dhan/live-orders`, `/reconcile`) 1:1 against every
  `psRequest(...)` call in `positionStocksApi.ts` — no drift, all present
  on both sides.
- Read `/status`'s full response builder against `ScalpStatus` — all
  fields (gate state, `pipeline_config`, `shared_order_budget`,
  `circuit_breaker`, `ws`, risk-config snapshot) match what the frontend
  types and Overview/Pipeline subtabs actually consume.
- Verified `pyflakes` + `py_compile` clean on `main.py`, `orders/entry.py`,
  `orders/reconcile.py`, `models.py`.
- Verified `npx tsc --noEmit` clean across the whole frontend.

## Not changed / left as-is

- `execution/dhan_client.py`, `screening/`, `capital/`, `feed/`,
  `resilience/`, `auth/` — not re-read line-by-line this pass; sessions
  19/19b/19c/19d/21/22 already covered these in depth and this session's
  scope was specifically the order-id gap plus a field-for-field check of
  the tab's own data surface (`main.py` + frontend), not a full re-audit
  of every module again.
