# Group 345 - fixes from the review of the 13 Oct log taken after the group 344 deploy

Verification status: every changed file passes `py_compile`. The sandbox this was built in cannot reach PyPI (403 by policy), so **pytest, sqlalchemy,
httpx and fastapi were not installed and the test suites were NOT run**. New / changed tests were written but are unexecuted. The context-local counter
pattern (#5) was checked with a stand-alone asyncio script. Run the three suites (position-stocks, real-trade, market-data, api-gateway,
analysis-intelligence) before deploying.

| # | Issue | Change |
|---|-------|--------|
| 1 | AVTNPL breakeven stop rejected every ~6 s | `position-stocks/orders/breakeven.py`: a rejected modify backs the position off 60 s (doubling to 15 min), one-line warning instead of a traceback, one Telegram notice per position; ticks older than 10 s are not trusted; when the price has fallen back under the buffered entry the original stop leg is left alone instead of being pushed to "one tick under the market". |
| 3 | Held-position quotes slow, `fresh 0` | `market-data/dhan_data/live_poller.py`: held positions (the AngelOne budget's POSITION set) plus `DHAN_POLLER_ALWAYS_SYMBOLS` are polled every tick on the demand lane, outside the 900-symbol rotating slice. The 6 `/live-quote` + 5 `/quote` per-cycle fall-backs on the caller side should disappear once held rows are fresh in memory; confirm in the next log. |
| 4 | Event-service timeouts | `analysis-intelligence/event/main.py`: one fetch per symbol at a time (callers join the one in flight, so the gate retry no longer starts a second fetch); `/check` yields to live `/events/*` requests and stops after `CHECK_MAX_SECONDS` (default 240). Whether EMIL's news check succeeded is still not provable from the log. |
| 5 | Surprise scan counters wrong when scans overlap | `api-gateway/surprise_scanner.py`: counters, prefetch dict and per-symbol deadline are context-local per scan. The 20 s overrun (bulk prefetch time) is NOT changed. |
| 6 | Dhan breakdown line unreliable | `market-data/dhan_data/quotes.py`: each `get_quotes` call fills its own dict; `last_call_breakdown()` returns the calling thread's call. |
| 8 | Board backfill every ~9 s | `position-stocks/feed/bar_backfill.py`: 6 symbols per pass (was 12); empty / failed symbols held `BOARD_BACKFILL_EMPTY_RETRY_S` (1 h); a batch that is >= 75 % empty pauses the backfill `BOARD_BACKFILL_PAUSE_S` (5 min). |
| 9 | ~170 volume-shock symbols re-judged every cycle | `real-trade/candidate_engine/candidates.py`: volume-shock rejects carry a kind and are cached (`vs:` keys; price floor / ATR 1 h, return / volume `REJECT_CACHE_VS_S` 5 min); the Rs 20 price floor is judged on the quote before any history call. |
| 10 | Deterministic rejects retried every ~11 s | `position-stocks/main.py`: PRICE_GUARD / RANGE_GATE / PENNY_STOCK rejects leave the candidate list for `GATE_REJECT_COOLDOWN_S` (60 s). |
| 11 | Watchlist re-queues the same ~20 symbols | `real-trade/entry_engine/entry.py`: a row just queued is parked `WATCHLIST_REQUEUE_COOLDOWN_S` (300 s). |

Not changed, and why:
- #2 RAMRAT: the exit fill must be checked in Dhan by hand; the PENDING_RECONCILE marker is correctly kept.
- #7 Hot Picks warm-up: group 344 already stamps snapshots; the log says it works from the next restart.
- #12 AngelOne feed cycle 30 s, #14 `scan/universe` at boot: warnings / minor, tuning only.
- #13 Oracle commit blocking api-gateway's loop: the log excerpt has no stack, so the call site cannot be named. Send the `loop_watchdog` stack lines.
- #15 BIRET / HEG have no AngelOne token (data, not code); #16 NSE endpoints dead; #17 news feeds degraded (fallbacks work); #18 GMRAIRPORT held back by the 10 s price-age limit (guard working); #19 scanners (harmless).
