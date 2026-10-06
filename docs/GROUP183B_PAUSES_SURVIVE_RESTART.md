# Group 183b - dead-symbol and no-history pauses survive a restart (real-trade-service)

Cumulative on group 183. This is "item 7" of the open list (which pause should survive a restart). I did both, because the
cost is the same and each redeploy otherwise asks once more about every symbol already known to have no price or history.
Rebuild: `docker compose build real-trade-service && docker compose up -d`.

## What was wrong
- group 160 (`market_feed/feed.py`): a symbol with no price is paused 30 min, doubling to 6 h. State was per process, so after
  a restart each such symbol was asked again three times before it was paused again.
- group 172 (`candidate_engine/candidates.py`): a symbol with no daily history is not asked again for 6 h. Same reset.

## Fix
- New `resilience/pause_state.py`: saves `{symbol: {"u": wall-clock deadline, "m": miss count}}` in `trade_resilience_cache`
  (same table and save/load pair as the ATR cache; keys `market_feed:dead_symbols`, `candidates:nohist`). Deadlines are stored
  as wall-clock time and converted back to monotonic on load. Expired or malformed entries are dropped; at most 2000 kept.
- Writes are debounced (one per key per 5 s) and made in a short daemon thread with its own DB session, so the event loop
  never waits on the DB. Nothing is written until the startup load has run, so tests and one-off imports never touch the DB.
- `main.py` startup restores both after the ATR cache warm-up and before auto-pilot starts. One INFO line per pause type when
  something was restored.
- A price that clears a saved pause, and a good history answer that clears a no-history pause, are saved too.
- Behaviour while running is unchanged. The miss count is restored, so the doubling backoff carries on where it was.
- `PAUSE_STATE_PERSIST=0` = per-process as before; `PAUSE_STATE_FLUSH_DELAY_S` (default 5).

## Not covered (still per process)
- Misses counted but not yet paused (1-2 in a row). They are not worth a write.
- market-data-service's own 1 h "no data" memory for a symbol (`/history` 404 cache) and api-gateway's caches. They are
  separate services and ask Yahoo at most once per boot per symbol (3 calls in your 2026-10-06 log).

## Tests
New `tests/test_group183b_pause_state_persist.py` (20 cases): helper round trip, expiry/garbage filtering, cap, never raises,
debounce and off switch, flush with own session, snapshot of paused symbols only, nothing written before startup load,
restore of a running pause with its miss count, expiry at the deadline, removal saved when a price arrives, switches off,
the same for the no-history pause. No pytest/sqlalchemy in my sandbox: all 20 ran through a stand-in runner with stubbed
modules and passed. The existing group 160/172 test files could NOT be run here (they need httpx/sqlalchemy), so run them on
the VM: `cd services/real-trade-service && python3 -m pytest tests/test_group183b_pause_state_persist.py tests/test_group160_dead_symbol_pause.py tests/test_group172_volume_shock_history_reasons.py -q`

## How to confirm on the VM
After a restart, look for `feed: restored N paused no-price symbol(s) from the DB` and
`volume_shock: restored N no-daily-history pause(s) from the DB`. Both only appear if something was paused when it stopped.
