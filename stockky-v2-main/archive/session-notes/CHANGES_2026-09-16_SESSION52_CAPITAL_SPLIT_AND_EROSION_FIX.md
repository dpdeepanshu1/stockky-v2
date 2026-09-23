# Session 52 (2026-09-16) — Capital-split fix confirmed + capital-erosion follow-up fix

## Context

Live evidence (`GET /ledger` on position-stocks-service, `GET /status/REAL` on
real-trade-service) showed:

- real-trade-service: `current_equity` ₹14,579.96, `cash_available` ₹943.93,
  ~₹13,636 tied up across 9 open REAL positions (~93.5% of total equity).
- position-stocks-service: `total_allocated_capital` ₹52.45,
  `last_synced_from_broker_at` over an hour stale.

Candidate Log showed a 100% `SKIPPED:INSUFFICIENT_CAPITAL` rate despite a live
screener finding real 1m-window candidates.

## Root causes

1. **[CRITICAL] The 50/50 split was only enforced on one side.**
   position-stocks-service's `capital/ledger.py` sized its pool as 50% of
   Dhan's current free cash. real-trade-service's `execution/equity_sync.py`
   set its own `cash_available`/`current_equity` from the FULL, uncapped Dhan
   balance and sized every trade off that — nothing reserved position-stocks-
   service's half.
2. **The scalp pool's capital never auto-refreshed.** `ledger.sync_from_broker()`
   was wired only to the manual `POST /ledger/sync` admin route.

## Fix (Option A)

- `real-trade-service/config.py`: new `REAL_TRADE_CAPITAL_SHARE_PCT` (default
  50.0).
- `real-trade-service/models.py` + `db.py`: new
  `trade_accounts.broker_cash_available` column (idempotent migration) holding
  the raw, uncapped Dhan balance alongside the now-capped `cash_available`.
- `real-trade-service/execution/equity_sync.py`: `cash_available`/
  `current_equity` now capped at this service's own `CAPITAL_SHARE_PCT` share
  of Dhan's live free cash.
- `real-trade-service/risk_engine/engine.py`: new `capital_share_cap` check —
  rejects any new BUY if this service's total exposure (open positions + the
  proposed order) would exceed its share of the shared account's true total
  (`broker_cash_available + open_positions_market_value`).
- `real-trade-service/entry_engine/entry.py`, `manual_engine.py`, `main.py`:
  wired the new fields into every risk-check call site. Along the way, fixed a
  separate pre-existing bug: `manual_engine.py` (and the admin dry-run
  endpoint) never set `cash_available` at all, so every manual REAL BUY
  confirmation was silently rejected by the cash-cap check regardless of
  actual balance.
- `position-stocks-service/main.py`: `ledger.sync_from_broker()` now runs at
  the top of every trading cycle (worker thread), not just on manual trigger.

Verified against live numbers before packaging: correctly **rejects** new
REAL entries while real-trade-service is over its 50% share, correctly
**no-ops** for DEMO (never synced from Dhan), correctly **passes** for a
healthy account under its cap.

## Follow-up fix found this session: capital-erosion bug in position-stocks-service's own ledger

While re-checking the pool-sizing math for correctness, found that
`capital/ledger.py`'s `sync_from_broker()` unconditionally set
`total_allocated_capital = scalp_alloc` (50% of Dhan's *current free cash*
only) on every sync — with no credit for capital this pool already has
committed to its own open positions. Since placing an order spends Dhan free
cash, the next sync would compute a smaller `scalp_alloc` and use it as the
new pool size — silently shrinking the pool's risk-sizing baseline
(`risk_rupees = total_allocated_capital * RISK_PER_TRADE_PCT`) on every cycle
a position stayed open. This is the same one-sided-split bug as issue #1
above, just on position-stocks-service's own side, and not yet visible today
only because `Positions (0)` means there's nothing open to erode against yet.

**Fix:** `total_allocated_capital` now adds back `capital_risked` for this
pool's own `OPEN`/`EXIT_LEGS_REJECTED` positions (a cost-basis figure already
tracked per position — no live-LTP dependency needed), so it reflects the
pool's TRUE 50% share (idle free cash + its own deployed capital) regardless
of how many of its own positions are open. `available_capital`'s reset logic
was adjusted to match: reset to the full `total_allocated_capital` only when
no positions are open (where it still equals `scalp_alloc`); the "stuck at
zero" fallback still resets to the free-cash slice only, since committed
capital is genuinely not available.

`py_compile` + `pyflakes` clean on `services/position-stocks-service/capital/ledger.py`.

## Files changed this session

- `services/position-stocks-service/capital/ledger.py`

(All Option A files from earlier in this session were already present in this
upload; see the writeup above for what they contain.)

## What to expect after deploy

- real-trade-service stops opening new positions until enough of its existing
  9 close to bring it back under 50% of total account value.
- position-stocks-service's `/ledger` reflects a live, non-stale number every
  cycle, and — once real-trade-service's cap frees up real headroom — a much
  larger scalp pool than ₹52.45.
- Once position-stocks-service actually opens its first live positions, its
  pool will no longer shrink itself cycle-over-cycle just because capital is
  deployed.

```bash
curl -s http://localhost:8006/ledger | python3 -m json.tool
curl -s http://localhost:8005/status/REAL | python3 -m json.tool
```
