# Group 340 — stop integrity, candle-history levels, share rounding, scalp target cap (2026-10-12)

Source: the 2026-10-12 review of 12 intraday position-stocks trades (7 wins / 5 losses, ~+₹107 gross) and the audit that followed.

## What changed (position-stocks-service unless noted)

| # | Change | Where | Switch (default) |
|---|--------|-------|------------------|
| 1 | **Stop read-back**: after every trailing / breakeven stop move, the Super Order is read back from Dhan; a move Dhan does not show is *not* saved (alert + retry backoff). Unreadable book = fails open, as before. | `execution/dhan_client.py` `verify_stop_leg`, `orders/trailing.py`, `orders/breakeven.py` | `STOP_VERIFY_ENABLED` (on) |
| 2 | **Stop-breach watchdog**: price ≥1 tick under an OPEN position's stored stop for 20 s on a fresh tick → close at market (`STOP_BREACH_EXIT`) + Telegram alert. Runs in the fast reconcile loop after the trailing stop. | `orders/stop_watchdog.py`, `main.py` | `STOP_WATCHDOG_ENABLED` (on) |
| 3 | **Candle history for the volatility stop**: thin tick buffer is topped up from the bar store, then one cached market-data `/history` call (2 s timeout, misses cached) instead of falling to the 2 % stop / ~4 % target floor. | `orders/adaptive.py` | `ADAPTIVE_HISTORY_ENABLED` (on) |
| 4 | **Scalp target cap**: target above 2.5 % is cut to it; if R:R falls under 1.5 the stop is tightened to target/1.5 (never under 0.8 %, never wider). | `orders/adaptive.py` `apply_target_cap` | `SCALP_TARGET_CAP_PCT` (2.5; 0 = off) |
| 5 | **Share rounding**: round up one share when ≤10 % over budget; skip (not force 1 share) when one share costs >1.5× the budget. | `orders/sizing.py`, `orders/entry.py` | `SIZING_ROUNDING_ENABLED` (on) |
| 6 | **Single source of trade statuses**; `STOP_BREACH_EXIT` added (frontend type + colour too). | `orders/statuses.py`, `orders/reconcile.py` | — |
| 7 | Reconcile stores the tick-rounded stop/target it really sends (DWARKESH card 47.13 vs trigger 47.21). | `orders/reconcile.py` | — |
| 8 | Reconcile logs each closed-trade fill correction once per process, not every 5 min. | `orders/reconcile.py` | `RECONCILE_LOG_ONCE` (on) |
| 9 | Optional gates, **shipped off**: cheap-stock floor and ordinary-day entry cut-off. | `orders/entry.py`, `orders/calendar_gate.py` | `ENTRY_MIN_PRICE` (0), `SCALP_ENTRY_CUTOFF_IST` ("") |
| 10 | Profitable `STOP_HIT` is labelled `TRAIL_STOP` in the Positions tab. | frontend `PositionStocksTab.tsx` | — |
| 11 | **real-trade-service**: imported demat holdings' target capped at 2.5× the stop distance. | `portfolio/portfolio.py`, `config.py` | `IMPORT_TARGET_MAX_STOP_MULT` (2.5; 0 = off) |

`tests/conftest.py` switches 1–5 and 9 off for the older tests; `tests/test_group340.py` (position-stocks) and
`tests/test_group340_import_target_cap.py` (real-trade) turn them on.

## Verification
- position-stocks-service: 3599 passed (3559 before + 40 new). real-trade-service: 4441 passed, 1 skipped. `tsc --noEmit` clean.
- NOT live-tested: the Dhan order-book field names used by the read-back (`legDetails[].legName == STOP_LOSS_LEG`, `triggerPrice`/`stopLossPrice`)
  follow the existing reconcile reader; if Dhan's row has none of them the read-back returns "unknown" and behaves exactly as before.

## Before enabling on the VM
1. `bash run_tests.sh`.
2. Deploy, watch one session. Log lines to look for: `TRAILING_STOP: ... NOT confirmed`, `STOP_WATCHDOG:`, `adaptive: target cap`, `ONE_SHARE_EXCEEDS_BUDGET`.
3. Any switch above can be turned off with its env var without a rebuild of code.
4. Still to check on the VM (not code): `BROKER_STOP_ENABLED` for real-trade-service (reported `False`), and the cancelled SELL ₹58.99 in Dhan's order list for FEDDERSHOL's root cause.
