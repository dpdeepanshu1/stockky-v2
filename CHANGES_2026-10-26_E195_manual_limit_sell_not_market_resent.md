# E195 - a manual LIMIT SELL that did not fill was cancelled and then sent again as a MARKET sell

Includes E194 (and E193 - E184).

## What I read
`services/real-trade-service/manual_engine.py::evaluate_manual_order` (BUY preview / confirm / lost-reply recovery, SELL preview / confirm, pending-sell guard, `_send_real_sell` hand-over) and
`_account_state`, plus what a manual SELL becomes afterwards: `_send_real_sell` (`valid_until` for LIMIT), `expire_stale_exit_orders`, `record_real_exit_sent`, and reconcile's `_repair_orphaned_pending_exits`.
Also the VM boot log you attached (2026-10-26 05:39 UTC): healthy; the two items I first suggested (dead / frozen news feeds, breakeven modify rejection) are already handled by groups 341 / 346 / 378 and 345, so no change there.

## Finding (fixed)
`_send_real_sell` gives every LIMIT sell `valid_until = now + EXIT_LIMIT_VALIDITY_MINUTES` (default 3). `expire_stale_exit_orders` then cancels it at Dhan and resends the remainder as MARKET. That is right for the
target-hit partial (its target is already cleared, so nothing else would ever sell the shares) but the pass matched EVERY expired SELL, including one typed in the Trade ticket
(`execution_source="MANUAL"`, `exit_reason="manual_sell"`). A manual partial LIMIT sell ("sell 4 of 10 at 112", price above the market) was therefore cancelled after 3 minutes and sold at market, below the limit the user chose.
A full-size manual LIMIT sell was not resent only by accident (its position is PENDING_EXIT, so the position lookup found nothing and logged "nothing to do"); reconcile's self-heal then restored the position.

## Change (`exit_engine/exit.py`)
- `_is_manual_sell_order(order)`: `execution_source == "MANUAL"` or `exit_reason == "manual_sell"`.
- `expire_stale_exit_orders`: an expired manual LIMIT sell is cancelled and marked EXPIRED as before, but NOT resent; the order event says so, one Telegram notice says it was not sold and can be placed again, and the position is back under its stop / target
  watch (OPEN / PARTIALLY_CLOSED partials never left it; a full-size one is restored by the existing reconcile self-heal). Target-hit partials are resent exactly as before; a failed Dhan cancel still leaves the order PLACED.
- No new environment variable.

## Read and fine (no change)
BUY: symbol / side / qty / order-type validation, intraday-restricted pre-check, tick rounding, stop-below-entry check, risk engine wiring, MARKET price 0, the E176 lost-reply recovery and symbol-claim release, auto-disarm on invalid IP.
SELL: qty capped to qty_open, pending-sell guard, `full` flag, LIMIT price passed through.
Not changed, your call: a manual BUY accepts a target at or below the entry price (risk-reward shows negative; no check rejects it).

## Tests
- `tests/test_e195_manual_limit_sell_not_market_resent.py` (6): all pass on the fix; 4 fail on the unchanged E194 `exit.py` (the 2 that pass pin: a target-hit partial is still resent as MARKET, a failed cancel leaves the order PLACED).
- `tests/test_exit_expire_stale_exit_orders.py` (existing) still passes.
- Whole real-trade suite here: 5,408 passed, 2 skipped, 4 failed. The 4 are `tests/test_oracle_compat.py::TestBuildOracleEngine` (`oracledb` is not installed in this sandbox; unrelated).
- `docs/ENV_REFERENCE.md` regenerated (it embeds source line numbers); `--check` passes.

    bash scripts/docker_test.sh real-trade-service
    python3 scripts/gen_env_reference.py --check
