# Group 226 - Hot Picks warm-up re-poll, a correction to group 225's back-pressure, and a "why no trades" report

Cumulative on group 225. Item 5 of the 2026-10-07 list, plus one fix to group 225 found while looking at "no BUY/SELL today".
Rebuild: `docker compose build real-trade-service && docker compose up -d` (market-data-service unchanged).
Files changed: `real-trade-service/market_feed/feed.py`, `watchlist_engine/sources.py`, `scripts/no_trade_diagnosis.py` (new),
tests `test_group226_tier1_hot_picks_warmup.py` (new, 8), `test_no_trade_diagnosis.py` (new, 5), `test_group225_priority_lane_backpressure.py` (+2).

## 1. Correction to group 225 (feed.py) - it could starve ENTRY candidates
Group 225 switched off per-symbol lookups for ALL non-priority batches for 20 s whenever the priority lane failed. An entry cycle prices
at most 20 candidates (below `FEED_BULK_MIN_SYMBOLS`, so per-symbol), so while any held symbol could not be priced (for example one
market-data cannot price at all) every candidate got "No current price available" and no BUY could happen. Now:
- the pause applies only to LARGE batches (more than `FEED_BULK_MIN_SYMBOLS`, i.e. the watchlist poll); entry batches are never paused;
- distress is marked only when a held symbol is STILL unpriced after bulk-first, per-symbol and the scaled bulk retry (not when the
  last-resort bulk call recovered everything).
If you deployed group 225, deploy this one before judging today's entries.

## 2. Item 5 - first watchlist refresh after a boot
api-gateway answers `/stockky-hot` at once with empty buckets and `"warming": true` while its Hot Picks cache is cold. Tier 1 read that as
"no catalysts", fell to Tier 2/3 and the cycle ran without the Hot Picks. Now an empty answer flagged warming is re-polled
(`WATCHLIST_TIER1_WARMUP_RETRIES`=3 times, `WATCHLIST_TIER1_WARMUP_WAIT_S`=8 s apart, 0 retries = off); a ready answer replaces it
(the IPO part is kept); a failing re-poll gives up without raising; one waiting spell per `WATCHLIST_TIER1_WARMUP_MIN_GAP_S`=120 s so a
slow Hot Picks job cannot add ~24 s to every cycle. A warming answer that already lists stale picks is used as before.

## 3. Why no BUY / SELL today - what the code says, and the report to run
I cannot see today's DB or logs, so this is the list of what can stop trades, from the code:
- BUY needs: gate armed AND auto-pilot on for REAL (admin-session expiry or the Dhan token expiring auto-disarms it); not before 09:30 IST
  (group 220 opening guard); candidates queued at all (Tier 1 empty at boot, "Insufficient daily history" rejections, group 224); a live tick
  for the candidate; then regime (Nifty score vs adaptive gate), drift, R:R >= 2.0, cost floor, risk engine, cash, Gate 6 ranking.
- SELL: the fast exit tick runs even when disarmed, but it needs a price. At 09:43 the six held symbols could not be priced, and each cycle
  wrote HOLD "No current price available this cycle - skipping evaluation". That was the real sell blocker in the boot log (group 225 addresses it).
Run, inside the container: `docker compose exec real-trade-service python scripts/no_trade_diagnosis.py` (`--mode REAL --date 2026-10-07 --top 25`).
It is read-only and prints the gate state, candidates by source, ENTRY and EXIT decisions grouped by reason (numbers blanked), orders, open
positions and watchlist counts, so the top line of the ENTRY/EXIT sections is the gate that stopped trading.

## Not changed
- No gate thresholds were touched. If the report shows the regime gate or R:R floor as the top reason, that is a tuning decision, not a bug.
