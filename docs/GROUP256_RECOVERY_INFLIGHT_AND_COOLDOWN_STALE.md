# Group 256 - recovery measurement fix, and no per-symbol Yahoo fallback during an AngelOne quote cooldown (2026-10-08 ~14:15 IST log)

## 1. Candle "recovery" was measured wrongly (market-data-service)
Log: `candle calls are answered again, 1s after the first 403` under a 30 s cooldown, then the ladder kept climbing. A call that was
already in flight when the 403 arrived came back normally and was taken for the recovery.
- `angelone_budget.note_candle_ok(sent_at=None)`: counts as recovery only when the call was SENT after the candle cooldown ended
  (no `sent_at`: the answer must at least arrive after it). Anything else leaves the run open.
- `angelone_client.get_candles` records the send time and passes it.

## 2. Quote cooldown no longer floods Yahoo (market-data-service main.py, real-trade-service feed.py)
Log: a quote 403 (30 s cooldown) sent every symbol AngelOne-first could not price down the Yahoo path: hundreds of
`AngelOne-first did not price X (angelone_quote cooldown) - using the Yahoo path`, saturated yfinance bucket, one 18 s
`yf.download` timeout (502), ~100 ReadTimeouts in real-trade-service.
While AngelOne's quote cooldown runs, for symbols NOT held (POSITION lane):
- `/quote/{symbol}` and the leftovers of `/quotes/bulk` reuse the cached / last-good row if it is <= `QUOTE_COOLDOWN_STALE_MAX_AGE_S`
  (default 180 s). The row keeps its REAL `fetched_at` and is tagged `source="stale_cooldown(<orig>)"`.
- With no such row: `/quote` answers `source="cooldown_unpriced"`, price None (NOT negative-cached); `/quotes/bulk` leaves it out.
  `QUOTE_COOLDOWN_SKIP_YAHOO=0` lets those go to Yahoo as before.
- Held symbols, indices, closed-market answers (group233) and pre-open answers (group244) are unchanged. A held-check failure
  is treated as "held" (fail safe).
- `QUOTE_COOLDOWN_SERVE_STALE=0` turns it all off. Env reads are blank-safe.
real-trade-service `market_feed/feed.py`:
- `cooldown_unpriced` is not counted as a definite miss for the dead-symbol pause (group160).
- A `stale_cooldown(...)` tick keeps the real age of its price as `as_of` (not receipt time), so staleness checks see it; such ticks
  are never remembered as a symbol's last-good tick (`_STALE_TAGS`).

## Not changed
No Dhan integration; no limits, ladder values or lane budgets changed.

## Tests
`market-data-service/tests/test_group256_recovery_inflight_and_cooldown_stale.py` (29), `real-trade-service/tests/test_group256_cooldown_unpriced_and_stale_as_of.py` (6),
group255 tests adjusted for `sent_at`. Run `bash run_tests.sh` on the VM.
