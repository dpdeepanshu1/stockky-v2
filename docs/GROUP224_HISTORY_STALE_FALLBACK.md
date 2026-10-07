# Group 224 - a transient /history failure reuses the last good daily candles (real-trade-service)

Cumulative on group 223. Item A3 of the 2026-10-07 open-market log review.
Rebuild: `docker compose build real-trade-service && docker compose up -d`.
Files changed: `real-trade-service/candidate_engine/candidates.py` (`_fetch_history` split into a wrapper plus `_fetch_history_raw`, new
fallback helpers, one INFO line in the volume-shock summary), `real-trade-service/tests/test_group224_history_stale_fallback.py` (new, 11).

## What the log showed
Between about 09:15 and 09:25 IST market-data was saturated (boot sweep, movers sweep, 499-symbol feed poll, AngelOne `getCandleData` 403).
Real-trade's `/history` calls (42 s timeout) then failed with ReadTimeout:
- volume-shock: "daily history unavailable for 25 of 157 symbol(s) ... ReadTimeout x25" - DIVISLAB, NESTLEIND, DRREDDY, ETERNAL, DABUR, BSE, MCX and others
  were skipped, each logged as "Insufficient daily history";
- main track: 15 symbols "incomplete timeframe history ... skipped as 'cannot judge'" (HONASA, CASTROLIND, INDORAMA, MUKANDLTD ...).
Both paths go through `_fetch_history`. Nothing was cached or paused (timeouts are never "definite"), so the symbols were retried next cycle;
the cost was one lost cycle for a sixth of the volume-shock universe during the busiest minutes.

## Change
`_fetch_history` now remembers the last good answer per (symbol, period, interval). When a call fails for a transient reason (timeout, HTTP
403/429/5xx) and a copy younger than `CANDIDATE_HISTORY_STALE_FALLBACK_S` (default 1800 s, 0 = off) exists, that copy is returned.
- Only `1d`, `1wk` and `1mo` candles are eligible (intraday bars go stale too fast).
- Definite answers never fall back: an empty answer, HTTP 404/400 and "short history" keep their existing meaning and 6 h no-history pause.
- The failure reason is still recorded (`_HIST_REASON`), so the cycle's warning and the pause logic are unchanged; when the fallback was used the
  summary logs one INFO line `history: N call(s) answered from the last good daily candles ...`.
- The returned list is a copy; the store is bounded (4,000 keys, cleared when exceeded) and cleared by `clear_history_state()`.

## Not changed / limits
- A symbol that never had a good answer in this process (first cycle after a restart) still has nothing to fall back to. The store is memory-only.
- The last daily candle may be today's in-progress bar, up to 30 minutes old when reused. Averages and ATR barely move; shorten the TTL if you prefer.
- The reject text stays "Insufficient daily history ..." for symbols that still have no data (existing tests and counters depend on the phrase).
- The cause (market-data load at the open, items A1/A2) is not addressed here.

| Env (real-trade-service) | Default | Meaning |
|---|---|---|
| `CANDIDATE_HISTORY_STALE_FALLBACK_S` | 1800 | max age of the reused copy; `0` restores the old behaviour |

## Tests
`cd real-trade-service && python -m pytest tests -q`: 3477 passed, 4 failed, 1 error. The same 4 failures
(`test_group171_held_quote_calls.py`, order-dependent, pass when the file runs alone) and 1 teardown error
(`test_group172...test_note_history_reason_never_raises_and_is_bounded`, it sets `_HIST_REASON` to None) occur on the unmodified group 223 upload; they are not from this change.
