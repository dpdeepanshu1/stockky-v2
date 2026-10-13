# Group 347 - Dhan live_quotes timeout back-off (13 Oct 09:39 log)

Verification: `py_compile` on changed files, repo checks, and a stand-alone stub script for the back-off. **pytest was not run**
(PyPI is blocked in the sandbox). Run `services/market-data-service` tests (new: `tests/test_group347_live_write_backoff.py`).

| Issue | Change |
|---|---|
| `dhan live_quotes write: 83 of 158 rows written ... DPY-4024 call timeout of 8000 ms` right after boot | `dhan_data/live_store.py`: a timed-out chunk opens a back-off (`DHAN_LIVE_WRITE_BACKOFF_S`, default 20 s, 0 = off). While it lasts only priority (held-position) rows are written; other rows are not marked as sent and go out on the first write after it. `status()` shows `backoff_skipped_batches`. Default `DHAN_LIVE_WRITE_CHUNK` 150 -> 60 (also updated in `.env.example` and `.env.oracle.recommended`) so one transaction is shorter. |

Checked, no change:
- **Reconcile "exit fill taken from today's order book" (TECHM, FSL, BRIGADE, ...)**: `_exit_fill_from_orderbook` reads a real
  `averageTradedPrice` for exactly one matching SELL; the "trigger" figure in the log line is only the reference price. Equal
  values are normal for stop/target legs that fill at the trigger. Not an approximation.
- **BIRET / HEG / HDFIRE / UNIVAFOODS**: not added to `KNOWN_DELISTED`. HEG and BIRET are, as far as I know, still listed, and a
  Yahoo "possibly delisted" message is not proof; adding them would stop trading them. Confirm on NSE first, then add to
  `KNOWN_DELISTED_SYMBOLS` (market-data `main.py`) AND `KNOWN_DELISTED` (api-gateway `symbol_aliases.py`; a drift test keeps them equal).

Still open (need your input or a longer log): #2 RAMRAT exit fill (Dhan), #5 Surprise scan 20 s overrun (no timing in this log),
and whether #12 / #13 / #17 worked (capture a log after 10:00 IST: grep `last_cycle_s`, `loop-lag`, `DPY-4024`).
