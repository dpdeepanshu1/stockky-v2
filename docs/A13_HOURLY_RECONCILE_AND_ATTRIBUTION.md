# A13 - hourly broker reconcile and trade attribution (2026-10-22)

## 1. Hourly broker-vs-book check (real-trade, read-only)
`execution/hourly_reconcile.py`, called from the fast exit tick (REAL only) when `HOURLY_RECONCILE_INTERVAL_MINUTES` (default 60) has passed.
It reads Dhan holdings + positions and the REAL order table and compares them with the book. It never writes to positions, orders or cash.

| kind | level | meaning |
|---|---|---|
| `missing_at_broker` | warn | book holds shares, broker holds none (older than `HOLDINGS_SYNC_GUARD_MINUTES`) |
| `book_over_broker` | warn | broker holds fewer shares than the book |
| `stale_order` | warn | REAL order PLACED / PARTIAL for more than `HOURLY_RECONCILE_STALE_ORDER_MINUTES` (90) |
| `broker_over_book`, `broker_only` | info | broker holds more / symbols the book has no row for. The Dhan account is shared with position-stocks, so these are listed, never alerted |

Telegram: one message when the warn set is new or changed, again every 4 h while it persists, and one "agree again" message when it clears.
`GET /reconcile-report/REAL` returns the last report; `?run=true` runs one pass now. A broker or DB error gives a report with `error` set and never reaches the tick.
The per-cycle sync (`holdings_sync_reconcile`) still does the fixing (ghost close, qty cap); this pass reports what it cannot fix and what it never looked at.

## 2. Attribution fields on trade records
Cause of the empty fields: `source_tab`, `entry_decision_label` and `entry_conviction_score` are copied to the position only when the BUY fill creates the row.
A position first created by the broker-import path (the group-438 race) keeps them NULL even though the entry order and the candidate recorded them.
`trade_records._attribution` now reads position -> entry order -> candidate; a manual entry with nothing recorded is `manual`. `attribution_source` says where each value came from.
The watchlist lookup also falls back to the candidate. `expectancy_report` carries `attribution_coverage` (filled / total per field, all trades and system-entered only).

Not changed on purpose: the position columns themselves. They also drive exit profile (`source_tab`) and overnight-hold eligibility (`entry_decision_label`), so writing them
onto already-open imported rows would change live exit behaviour; the fix is on the read side only.
Still empty on old trades: `stop_basis` and the order-flow numbers, which are written at the entry decision only since groups 396 / 427.

Tests: `services/real-trade-service/tests/test_a13_hourly_reconcile_and_attribution.py` (13, pure).
