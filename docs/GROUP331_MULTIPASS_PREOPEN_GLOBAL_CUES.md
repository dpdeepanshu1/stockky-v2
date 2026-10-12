# Group 331 - multi-pass pre-open snapshot and global cues for the carry decision

Finishes the two items group 330 listed as open. Everything fails open and has its own env switch (see `.env.example`).

## 1. Pre-open snapshot: three passes (real-trade `entry_engine/preopen.py`)
* Passes at `PREOPEN_SNAPSHOT_TIME_IST` (09:08), 09:11 and 09:14. `PREOPEN_SNAPSHOT_PASSES_IST` (comma list) overrides.
* A later pass **adds new gaps and overrides an earlier reading, but never drops one**; a symbol with no fresh price at 09:08 gets two more chances.
* Caches (previous-day candles, VIX, results dates) are warmed on the first pass only.
* A restart at 09:12 runs one catch-up pass that counts as 09:08 and 09:11; nothing runs at or after 09:15.
* Per-pass coverage (`queued`, `priced`, `fresh_gap`) is kept and returned by the new `GET /preopen/status` (also `passes_done`, top gaps).
* `[preopen] LOW COVERAGE` WARNING when fewer than one in five queued names had a fresh price: the gap gate is then mostly blind that day.
* Still unverified live: whether the feed has a fresh price at 09:08-09:14. The coverage numbers will tell you on the first trading day.

## 2. Global cues in the carry decision (real-trade `entry_engine/global_cues.py`)
* Reads the day move of `GLOBAL_CUE_SYMBOLS` (default DAX, FTSE, Nikkei, Hang Seng, ES=F, NQ=F) once per carry batch, cached 5 minutes.
* A position is not carried when at least `GLOBAL_CUE_MIN_READINGS` (2) cues read and their average is <= `CARRY_GLOBAL_MIN_AVG_PCT` (-0.9), or any US-futures cue is <= `CARRY_GLOBAL_US_FUT_MIN_PCT` (-1.2). 0 turns a test off.
* Order inside `carry_context.reason`: Nifty, then VIX, then global cues, then news.
* GIFT Nifty is not included (no verified source).

## 3. market-data `GET /global-cues`
* `/quote` could not serve these: it appends `.NS` to `ES=F` and applies NSE-equity rules. The new route asks Yahoo directly for allow-listed symbols (`GLOBAL_CUE_ALLOWED`), caches each symbol (a miss too) for `GLOBAL_CUE_TTL_S`, honours the yfinance cooldown, and drops a market whose last bar is older than `GLOBAL_CUE_MAX_BAR_AGE_DAYS` (1; a market on holiday has no move today). Unreadable symbols are listed under `missing`.

## Check on the VM
```
docker compose exec market-data-service curl -s "localhost:8000/global-cues"      # rows for the six symbols; "missing" lists any Yahoo could not give
curl -s localhost:<real-trade-port>/preopen/status                                # after 09:15: passes_done 3 entries, coverage per pass
```
Tests: `bash scripts/docker_test.sh real-trade-service` and `bash scripts/docker_test.sh market-data-service`.
