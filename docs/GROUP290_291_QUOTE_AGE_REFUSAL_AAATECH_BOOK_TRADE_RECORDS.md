# Groups 290 and 291 - entries refuse an old price, dead-parent reconcile waits for a readable order book, per-trade record and expectancy report

Plan phase A (every price says how old it is, and entries refuse an old one), the AAATECH follow-up, and plan phase B
(measure before tuning). Builds on group 289 (`as_of` / `age_s` on every market-data quote).

## 1. Quote-age refusal (real-trade-service)

### Why
Group 289 made market-data say how old each quote is, but nothing used it. `market_feed.get_quote` (source 2, market-data
`/quote`) stamped every row with the time real-trade RECEIVED it, so a minute-old cached price looked brand new to the
risk engine's stale-data check and to every entry guard. Entry, stop and target are all built from `tick.price`, and a LIMIT
order is sent at it.

### What changed
- `market_feed/feed.py`
  - `_quote_row_as_of(q)`: the real time of a `/quote` row. `age_s` (a duration measured on market-data's clock) is preferred
    because it does not depend on the two hosts' clocks agreeing; the tz-aware `as_of` is the fallback. Negative, NaN, infinite,
    bool, non-numeric ages and naive `as_of` strings are not used; a future `as_of` is clamped to now.
  - `Tick.age_known` (default True). False only when the row said nothing usable about its age (older market-data build);
    `as_of` is then just receipt time and the tick says so. Kept on the `stale_last_good(...)` copies.
  - Order of trust for a `/quote` row: `age_s` / `as_of`, then group 256's `stale_cooldown` `fetched_at`, then receipt time.
    `fetched_at` alone is never trusted (naive UTC with different meanings in different services).
- `entry_engine/entry.py` - new **Gate 2b** in `evaluate_mode`, before any price, stop or target is computed:
  1. tick older than `ENTRY_MAX_QUOTE_AGE_S` (default **10 s**, `0` = off) -> one re-read through the priority lane
     (`get_quotes([symbol], priority=True)`); the newest reading replaces the tick, so the order uses it.
  2. still too old -> the candidate is **held**: it stays queued (`consumed` back to False, no decision row, one log line per
     symbol per window) and is judged again next cycle, for up to `ENTRY_QUOTE_AGE_HOLD_MIN` minutes (default 10) after it was
     received. After that it is consumed as an ordinary WAIT naming the newest age ("Price too old to enter on: ...").
     `ENTRY_QUOTE_AGE_HOLD_MIN=0` = never hold.
  3. unknown age (`age_known` False or no datetime `as_of`) follows `ENTRY_QUOTE_AGE_UNKNOWN`: `allow` (default, the
     behaviour before this group, so an older market-data build does not stop every entry) or `refuse`.
  - The helpers never raise; a bug in them means "no objection", never a stopped cycle.
- Not changed on purpose: `manual_engine.py` (a human confirmed that price), and SELL/exit paths (a stale price must never
  block an exit). The watchlist trigger already had `WATCHLIST_MAX_TICK_AGE_S` (30 s); it now sees the honest age too.

### Rollout
- Switch off: `ENTRY_MAX_QUOTE_AGE_S=0`. Strictest: `ENTRY_QUOTE_AGE_UNKNOWN=refuse` (only once market-data with group 289 is deployed).
- Deploy market-data (group 289) BEFORE or with this zip; against an older market-data every `/quote` price is `age_known=False`
  and is allowed through by default.
- Watch the `held back, price from ... is Ns old` log lines on the first live day.

## 2. AAATECH follow-up: an unreadable order book no longer turns a filled trade into ERROR (position-stocks-service)

### Why
Group 276 stopped a REJECTED super-order PARENT from booking a filled trade as ERROR by looking for the BUY in today's order
book. Two holes remained, both ending in ERROR / Rs0 with capital and symbol lock released and no exit placed:
1. the order book could not be READ (Dhan 403 "exceeding access rate" is routine) and was treated like an empty book;
2. the book came from the 20 s share and could predate the fill.

### What changed (`orders/reconcile.py`)
- `_order_book_read(db, force=False) -> (rows, readable)`; `readable` is False only when the Dhan call failed. A failed read is
  never cached. `_cached_order_list` keeps its old contract.
- The dead-parent branch reads the book with `force=True` (fresh). If it cannot be read, the position stays OPEN and is checked
  again next pass, for at most `DEAD_PARENT_BOOK_WAIT_S` seconds from the first unreadable pass (default **180**, `0` = old
  behaviour; blank / bad / NaN -> 180, negative -> 0). After that the old ERROR path runs, and
  `POST /reconcile/repair-dead-entry-errors` can still undo a wrong one. A readable book resets the clock.
- Everything else is unchanged: a readable book that proves nothing is still ERROR at once; `DEAD_PARENT_FILL_CHECK=0` still
  means no lookup at all.
- One existing test (`test_group192_entry_reject_learning.py::test_order_book_failure_still_closes_the_position_without_a_reason`)
  pinned the old behaviour; it now sets `DEAD_PARENT_BOOK_WAIT_S=0` to keep pinning exactly that.

## 3. Per-trade record and expectancy report (real-trade-service)

New `trade_records.py`; no new table, no migration. One record per CLOSED, system-entered position (broker-imported holdings
are left out), built on read from `trade_positions`, `trade_orders`, `trade_fills`, `trade_decisions`, `trade_candidates`,
`trade_watchlist` entries and `trade_charges_ledger`.

- Orders are matched to a position by mode + symbol + time (the position with the latest `opened_at` not after the order's fill
  time, minus 2 min slack; a SELL up to 1 h after close still belongs to it). Same-symbol re-entries the same day do not mix.
- Record fields: tier / tier name, catalyst type and price, signal price, entry price, `entry_slippage_pct` (+ = paid above the
  signal), `entry_vs_catalyst_pct`, exit price (VWAP of the SELL fills), entry / exit time and entry hour (IST), held minutes,
  exit reason (last SELL's `exit_reason`; `manual` for a manual SELL without one), gross P&L, charges, net P&L.
- Charges: ledger actuals when the ledger covers EVERY matched order (`charges_source` `ledger`, or `ledger_estimated`); else
  the position's own round-trip estimate (`estimate`); else its `net_realized_pnl` (`net_realized_pnl`); else None.
- Unknown is None, never 0.
- `GET /positions/{mode}/records?days=&symbol=&limit=` - the records, newest first (days 1-60, limit 1-1000).
- `GET /positions/{mode}/report?days=` - expectancy, win rate, payoff ratio, charges, net P&L, average slippage and hold, grouped
  by exit reason, entry hour, tier, catalyst type, source tab and day; groups under 5 trades carry `low_sample: true`.
- Both are read-only; REAL needs the admin login like every other REAL route; a bad mode answers 400.

## Tests
- real-trade-service: `test_group290_quote_age.py` (67), `test_group290_trade_records.py` (47, including the real loader on
  SQLite and the real HTTP routes). Full suite 4030 passed, 1 skipped (`test_db_postgres_live.py` needs `pgserver`); 114 are new.
- position-stocks-service: `test_group291_dead_parent_unreadable_book.py` (26). Full suite 3134 passed (3108 before).
- Mutation checks: 20 deliberate breaks of the new code (feed ignores `age_s`, unknown age flagged known, bad ages accepted,
  limit loosened, no re-read, held candidate consumed, 0 no longer off, unknown policy inverted, stale share used, no
  deferral, default wait 0, failed read looks readable, slippage broken, partial ledger accepted, charges added, imported
  holdings counted, first SELL names the reason, orders matched to the wrong position, `low_sample` off, symbol filter
  dropped); every one was caught by the new tests.

## Still open
- Other price consumers (position-stocks entry, depth gate, `/quote` readers outside entry) do not yet refuse by `age_s`;
  real-trade entries are the first consumer.
- Daily report is on demand (the endpoint); nothing pushes it to Telegram or stores a daily snapshot yet.
- Order-update WebSocket shapes (groups 280/287/288) still need a live session before `RECONCILE_USE_ORDER_EVENTS=1`.
