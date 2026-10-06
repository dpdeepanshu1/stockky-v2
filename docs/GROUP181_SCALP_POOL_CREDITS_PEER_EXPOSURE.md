# Group 181 — scalp pool no longer shrinks when real-trade buys (position-stocks-service)

Carried-over item: "the scalp pool shrinking when real-trade buys".

## Cause
`capital/ledger.py::sync_from_broker` sizes the pool as `SCALP_POOL_CAPITAL_SHARE_PCT` (50%) of Dhan's FREE cash plus
its own committed capital. Free cash also falls when real-trade-service buys, so each real-trade buy cut the pool's
risk-sizing baseline (`total_allocated_capital`) by half of what real-trade spent, although the account's total value
had not changed and real-trade-service is itself capped at its own half. Example: 100k account, real-trade buys 40k:
free cash 60k, pool = 30k instead of 50k.

## Fix
real-trade-service already publishes its open-position market value to the shared exposure table every equity sync
(`capital/shared_exposure.py::get_other_service_exposure`, previously unused here). The pool now adds its share of that
back: `total = share% * free cash + own committed + share% * peer open-position value`.
- Capped at free cash + own committed capital, so the pool is never sized above what the account holds in cash.
- Fail-open: a missing/zero/unreadable exposure leaves the old figure.
- One INFO line (on change, else every `LEDGER_SYNC_LOG_EVERY_S`) shows the credited amount.
- `SCALP_POOL_CREDIT_PEER_EXPOSURE=0` restores the old figure.

## Things to know
- `available_capital` is still only reset from the new total when no scalp position is open (unchanged rule), so the
  larger baseline reaches spendable capital when the pool is flat; while positions are open it mainly affects risk sizing.
- The published exposure is real-trade-service's last equity sync; if that service is down the last value stays.
  The free-cash cap bounds the effect.
- Not changed: the pool still credits ALL its own committed capital, not half of it (existing, deliberate per the
  session52 comment); I did not touch that.

## Tests
`tests/test_group181_ledger_peer_exposure_credit.py` (13 cases): credit, cap, own-committed ceiling, switch, fail-open,
real shared-table read, before/after-buy equality, one log line. Sandbox: full position-stocks-service suite 2606 passed.

Rebuild position-stocks-service.
