# Group 211 - one shared AngelOne budget: priority lanes + one global rate-limit cooldown (market-data-service)

Cumulative on group 210. Item 2 of the open list ("AngelOne rate limit / feed poll / per-symbol /quote waves", see groups 199, 200 and 194's
"Not changed"). Rebuild market-data-service only.

## What was wrong
Every AngelOne caller in market-data-service (the 489-symbol feed poll, the ~2,500-symbol movers sweep, per-symbol `/quote`, `/quotes/bulk`,
`/history` candles) drew from the same `angelone_quote` (5/s, burst 8) and `angelone_candle` (1.5/s, burst 3) buckets with no idea who mattered
most, and each endpoint kept its OWN cooldown after a 403 "exceeding access rate". A 403 on candles did not stop quote callers; open-position
quotes could queue behind a background sweep.

## Changes
- New `angelone_budget.py`:
  - Lanes POSITION (held symbols) > CANDIDATE (`/quote`, `/quotes/bulk`, `/history`) > BACKGROUND (movers sweep, cold part of the feed poll).
    A lane takes a token only while the bucket still holds a reserve for the lanes above it: CANDIDATE keeps 25 % of the burst capacity free,
    BACKGROUND 50 %, POSITION 0. A lane that cannot get in within its wait is shed (caller falls back exactly as the fail-closed paths already did).
  - One global cooldown: a rate-limit answer from quote, quote(batch) or getCandleData makes every caller skip AngelOne for 30 s (doubling to
    at most 60 s on a repeat within 5 min). Late 403s from calls already in flight do not extend it. gainersLosers honours the cooldown but never
    starts one (its flat 403 can be an F&O access restriction).
  - POSITION symbols = open rows of `trade_positions` (OPEN / PARTIALLY_CLOSED / PENDING_EXIT) and `scalp_positions` (OPEN / EXIT_LEGS_REJECTED)
    in the shared DB (same engine as `live_quotes`, Postgres or Oracle), refreshed on a daemon thread every 15 s so no request waits on the DB.
    A missing table contributes nothing; an unreachable DB keeps the last known set.
  - "Hot" set: symbols looked up through single-symbol `/quote` in the last 120 s (newest first, capped).
- `rate_limiter.bucket_level()`: non-consuming peek at (tokens now, capacity).
- `angelone_client.py`: `get_quote` / `get_quotes_batch` / `get_candles` take `lane=`; all four endpoints skip during the global cooldown.
  `lane=None` keeps the old behaviour (apart from honouring the global cooldown).
- `angelone_ws_feed.py`: each cycle polls held symbols (POSITION), hot symbols (CANDIDATE) every cycle and the rest of the universe at most
  every `ANGELONE_FEED_COLD_INTERVAL_S` (30 s) as BACKGROUND; skips the whole cycle during the global cooldown. Failed-batch log now names
  the right symbol range.
- `main.py`: `_ao_lane()` classifies `/quote`, `/quotes/bulk` and `/history` lookups; the movers sweep runs as BACKGROUND;
  new `GET /angelone/budget` (cooldown state, per-lane admitted / shed / skipped_cooldown, held and hot counts, AngelOne buckets).
- Env (all optional, blank-safe, listed in `.env.example`): `ANGELONE_BUDGET` (0 = off), `ANGELONE_GLOBAL_COOLDOWN_S`,
  `ANGELONE_GLOBAL_COOLDOWN_MAX_S`, `ANGELONE_LANE_RESERVE_CANDIDATE`, `ANGELONE_LANE_RESERVE_BACKGROUND`, `ANGELONE_LANE_MAX_WAIT_S`,
  `ANGELONE_POSITION_LANE_REFRESH_S`, `ANGELONE_HOT_DEMAND_WINDOW_S`, `ANGELONE_FEED_COLD_INTERVAL_S` (0 = old poll), `ANGELONE_FEED_HOT_MAX`.

## Not changed
- api-gateway's own per-symbol `GET /quote/<SYM>` call sites are untouched; they now arrive as CANDIDATE and are shed to the Yahoo path
  when the bucket is low instead of queueing behind the feed.
- The AngelOne rate limits themselves.

## Tests
`tests/test_group211_angelone_budget.py` (63): cooldown trip / escalation / suppression, lane reserves and shedding, position-symbol loading
(both tables, missing table, no DB, background refresh), hot set, client skip / shed / trip on every endpoint, feed plan and cycles, `/angelone/budget`.
Sandbox: market-data 1017 passed. Not live-tested.

## Check on the VM after the rebuild (market hours)
`curl -s localhost:<market-data-port>/angelone/budget` - `position_symbols` should equal your open positions; `lanes.background.shed` may be
non-zero, `lanes.position.shed` must stay 0; one 403 shows as `trips: 1` (later 403s under `suppressed_late_403s`), not one cooldown per endpoint.
