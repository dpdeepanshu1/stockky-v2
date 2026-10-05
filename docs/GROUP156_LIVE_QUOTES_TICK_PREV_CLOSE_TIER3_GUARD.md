# group156 (2026-10-05) - live_quotes ticks carry the previous close, so the Tier-3 direction guard works

Cumulative on group155. Rebuild: `docker compose build real-trade-service && docker compose up -d`.

Source: the 2026-10-05 07:37 UTC VM log (item D22/D23 of the issue list built from that log).
Files changed: `real-trade-service/market_feed/feed.py` (+ one new test file).

## What the log showed (after group155 was already deployed)
Ten Tier-3 rows were QUEUED with a move of exactly 0.00 % (GLENMARK, HFCL, INOXINDIA, MAFANG, MASPTOP50, ONEPOINT, SAKAR,
SHADOWFAX, SHANKARA, SPORTKING), although the candidate engine's own quote said SAKAR -5.7 %, SHANKARA -5.2 %,
SPORTKING -6.2 % on the day. Other Tier-3 rows in the same pass were correctly held ("volume-shock stock is -5.19 % on the day").

## Cause
Group155's Tier-3 day-change check needs `Tick.prev_close`. Only the `/quote` and `/quotes/bulk` paths filled it. The first and
most common source, market-data `/live-quote` (the `live_quotes` table), built its `Tick` without it, so for those symbols the
guard hit its fail-open branch (`prev_close=None`) and queued the row. Group155's doc listed this as a known limit; the log shows
it is hit often (every symbol the AngelOne feed has a fresh row for).

## Fix (feed.py)
New `_lq_prev_close(lq, ltp)`: reads `ohlc.close` from the `/live-quote` answer. The AngelOne feed stores the broker quote's
`close` there, which is the previous session's close (market-data's movers sweep already reads it that way). If the close is
missing, not a number, <= 0, or exactly equal to the LTP (the writer falls back to the LTP when the broker sent no close), it is
treated as unknown and the guard stays fail-open as before. Never raises. Only the watchlist day-change guard reads it; nothing
that sizes or sends orders does.

## Tests
New `tests/test_group156_live_quotes_prev_close.py` (10 tests): tick carries the close, 7 missing/untrustworthy shapes give
None, helper never raises, and an end-to-end case (Tier-3 row, live_quotes tick -5.66 % on the day) is not queued. Two of the
tests fail on the group155 code. Full real-trade suite here: 2866 passed, 2 skipped, 4 failed, 1 collection error. The 4 failures
are `test_oracle_compat.py::TestBuildOracleEngine` and the error is `test_dhan_credentials.py` (module missing in my sandbox);
both are the same on the uploaded group155 zip.

## Judgement call to check
I am relying on AngelOne's `close` being the previous close during market hours (consistent with how the repo already uses it).
After the redeploy, a Tier-3 `SKIPPED ... on the day` line should appear for symbols that used to queue at 0.00 %. If a stock that
is clearly up on the day is held back, check that symbol's `/live-quote` `ohlc.close` against Dhan's previous close.

## Not changed (needs you, or a separate group)
Site unreachable (P0): the container layer was healthy, so check DuckDNS IP vs the VM public IP, host nginx, and the OCI security
list; nothing in code can fix that. Everything else on the issue list is still open (see the reply for the remaining list).
