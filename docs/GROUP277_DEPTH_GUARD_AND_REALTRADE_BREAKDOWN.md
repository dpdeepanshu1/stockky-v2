# Group 277 - Dhan depth guard on real-trade entries, real-trade trade breakdown

## Done
1. **real-trade `market_feed/feed.py`** - `Tick` now carries `spread_pct` and `book_value_5` (from market-data `/quote` and `/quotes/bulk`, which group 276 taught to emit them); stale-copy ticks keep them; `_safe_depth_num` rejects absent / negative / NaN / inf.
2. **real-trade `entry_engine/entry.py`** - `_watchlist_depth_reason`, called from `_watchlist_adverse_reason`: a row whose spread is above `WATCHLIST_MAX_SPREAD_PCT` (default 0.5, 0 = off) or whose best-5 book is below `WATCHLIST_MIN_BOOK_VALUE` Rs (default 0 = off) stays active and is not queued. Unknown depth never blocks; `WATCHLIST_ADVERSE_GUARD=0` still turns the whole guard off. Tests: `tests/test_group277_depth_guard.py`.
3. **real-trade `trade_breakdown.py` + `GET /positions/{mode}/breakdown?days=`** - closed trades by 30-min IST entry bucket, source tab and exit hour, with win rate, gross and net P&L and expectancy per trade (net where the row has it). Same idea as position-stocks `/trades/breakdown` (group 167). Tests: `tests/test_group277_trade_breakdown.py`.

## Checked, nothing to build
position-stocks already has the spread gate (MAX_SPREAD_PCT, mode-3 depth), opening gate, cost gate, loss brake, entry pauses, 30-minute stagnation exit and a trade breakdown; real-trade already has a daily loss cap (DEFAULT_MAX_DAILY_LOSS_PCT).

## Not done
See the reply: order-update websocket, live feed for exits, gateway/analysis/training off yfinance, delivery-holding stop review, per-trade slippage record, stagnation experiment.
