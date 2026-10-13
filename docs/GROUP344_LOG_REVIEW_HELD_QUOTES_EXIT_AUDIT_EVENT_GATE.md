# Group 344 - 13 Oct trading-day log review (10 items)

## Changes
| # | Item | Change | Where |
|---|------|--------|-------|
| 1 | Held-position quote path timing out (`/quotes/bulk ReadTimeout`, `DPY-4024`, `live_quotes read failed`) | Dhan live writer skips rows whose price is unchanged for `DHAN_LIVE_WRITE_REFRESH_S` (4 s; 0 = old behaviour); held symbols (sent by real-trade's priority lane via `priority: true` on `/quotes/bulk`, marked for 180 s) are written first in their own transaction and never skipped; `/live-quote` answers from the poller's newest row if at most `DHAN_LIVE_MEM_MAX_AGE_S` (6 s) old, before Oracle | market-data `dhan_data/live_store.py`, `main.py`; real-trade `market_feed/feed.py` |
| 2 | Exit prices on closed trades may be the trigger price | The "exit fill taken from today's order book" line logs once per position and fill (the 5-minute repair re-read it every time); a row whose booked exit equals the order-book fill is not looked up again (`EXIT_FILL_CONFIRM_ONCE`); `/reconcile/repair-closed` returns `exit_audit` (booked exit vs order-book fill per closed TARGET/STOP row, including rows skipped for entry reasons); a live close booked without any fill price logs a WARNING | position-stocks `orders/reconcile.py` |
| 3 | Entry without a news check (`event fetch failed ... ReadTimeout`) | Gate retries the event call once (`QUALITY_GATE_EVENT_RETRY_TIMEOUT_S`, 6 s); if it still fails and nothing is remembered for the symbol the candidate is rejected `EVENT_CHECK_UNAVAILABLE` (re-tried next cycle); after `QUALITY_GATE_EVENT_FAIL_OPEN_AFTER_S` (900 s) of unbroken failure it fails open and warns; `QUALITY_GATE_EVENT_FAIL_BLOCK=0` restores the old behaviour. Pre-open warm-up stops after `PREOPEN_WARM_BUDGET_S` (45 s) and at 09:15 (`PREOPEN_WARM_STOP_AT_OPEN`) so it no longer holds the event service for minutes | position-stocks `screening/quality_gate.py`; real-trade `entry_engine/preopen.py` |
| 4 | Held symbols possibly priced only by Yahoo | market-data `POST /internal/feed-coverage` (AngelOne token, Dhan security id, delisted list, fresh Dhan quote per symbol); real-trade asks it for the open REAL positions every `HELD_COVERAGE_CHECK_INTERVAL_S` (600 s) and logs a WARNING plus one Telegram alert per symbol per day for any with no primary feed. Report only | market-data `main.py`; real-trade `market_feed/held_coverage.py`, `execution/auto_pilot.py` |
| 5 | Hot Picks ~5 min warm-up, 3 x 8 s re-poll every cycle | While Hot Picks warms, today's saved Tier 1 snapshot (at most `WATCHLIST_TIER1_SNAPSHOT_MAX_AGE_H` = 8 h old, listing picks) is used with no waiting; the warming-empty answer is no longer saved over the snapshot; with no snapshot the wait gap doubles after each empty spell (cap 900 s) | real-trade `watchlist_engine/sources.py` |
| 6 | ~566 watchlist rows skipped every cycle, bulk list ~680 symbols | A row skipped for "below / above catalyst" is parked for `WATCHLIST_PARK_S` (300 s; 0 = off): still `active`, not priced or re-checked until the park ends. Other skip reasons are never parked | real-trade `entry_engine/entry.py` |
| 7 | Surprise scan 22-55 s, "1007 upstream calls" | Counters now real: `bulk_priced`, `cache_hits`, `single_calls`, `over_budget`, `unpriced`; `upstream_calls` = bulk requests + single calls. The per-symbol fallback has a `SURPRISE_SINGLE_BUDGET_S` (10 s) budget after the bulk prefetch | api-gateway `surprise_scanner.py` |
| 8 | AngelOne `getCandleData` 403 on the first call after a cooldown | First candle cooldown is `ANGELONE_CANDLE_COOLDOWN_BASE_S` (90 s, never below the shared base), then doubles to the existing candle ceiling | market-data `angelone_budget.py` |
| 9 | Dhan paused 30 s at boot (`db_unavailable`); partial pricing 19/95 | Transient credential read failures pause `DHAN_TRANSIENT_PAUSE_S` (5 s) and the negative creds cache follows it. The `quotes/bulk: Dhan priced X/Y` line now carries a breakdown (fresh / not in Dhan's scrip map / unanswered when the wait ended / Dhan paused). The cause of the partial pricing is not proven; the breakdown will show it | market-data `dhan_data/{config,client,creds,quotes}.py`, `main.py` |
| 10 | position-stocks sent HATHWAY (Rs10.6) through the quality gate | `MIN_STOCK_PRICE` / `ENTRY_MIN_PRICE` floor is applied before the gate (`PRICE_FLOOR_BEFORE_GATE`); skipped symbols are listed as `below_price_floor` in the cycle summary. Entry still enforces the floor | position-stocks `main.py` |

## Tests
New: `market-data-service/tests/test_group344_live_path.py` (17), `real-trade-service/tests/test_group344_log_review.py` (21),
`position-stocks-service/tests/test_group344.py` (11), `api-gateway/tests/test_group344_surprise_budget.py` (5).
Existing fixtures adjusted: market-data `conftest.py` (old 30 s candle base, clean live-writer state), real-trade `conftest.py` (park off, warm-up not
time-of-day dependent) and two fakes that now accept `priority`, position-stocks `conftest.py` (floor / event block off for older tests) and one event fake.
Results: market-data 1662, real-trade 4459 + 1 skipped, api-gateway 8617 + 5, position-stocks 3623 + 11 new with 3 failures that also fail on the group 343 zip
(`test_group322` silent-except pin 3 vs 1 in `orders/reconcile.py`, `test_reconcile ...[100.0]`, `test_ws_client ...max_ticks_backstop`).

## Not changed / not provable from the log
- Whether the six exit prices equal Dhan's fills: compare `exit_audit` from `POST /reconcile/repair-closed` (dry run) with Dhan's trade book; rows marked
  "no single matching SELL" are the ones to check.
- Cause of Dhan pricing only 19/95, 32/76, 30/64 symbols at times: needs the new breakdown line from a live session.
- Which symbols you hold: the held-symbol feed check names them in the log / Telegram.
- The 401s before login, NSE 403, dead news feeds, SHIVAMAUTO day-stats and `BROKER_STOP_ENABLED` warnings: expected (see group 342).
