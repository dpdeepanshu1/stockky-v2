# Group 360 - post-close `structure_only` refresh job + gateway counters

The previous session was cut off after writing `structure_refresh.py`; none of it was in the group 359 zip, so everything here was rebuilt and re-tested.

## Problem
Feed rows only got their flat `ms_*` keys as a side effect of repairing a missing RSI (g349). Most rows had no structure, or a days-old one, so the Buy Sniper gate, the PREPARE refresh notes (g358) and the feed coverage line all had "no opinion".

## api-gateway
- `structure_refresh.py` (new). Re-reads ONLY `GET {TECHNICAL_URL}/structure/{sym}` and replaces the `ms_*` group on the existing feed row. No fundamentals, events or quotes.
  - Selection by need: rows with no structure first, then rows whose `ms_asof` is older than the last COMPLETED session (`expected_bar_date`: today only after 15:30 IST + `STRUCTURE_BAR_CLOSE_BUFFER_MIN`, the same variable the technical service uses; weekends and NSE holidays skipped), then, with `force`, everything.
  - A missing / stale row already tried within `STRUCTURE_REFRESH_RECHECK_H` is skipped (stamp `ms_refreshed_at`), so a symbol the technical service cannot serve is not hammered all day.
  - Outcomes per symbol: `refreshed`, `unchanged` (same `ms_hash` + `ms_asof` + quality: only the stamp is written), `no_block` (reply had no valid block: old keys are KEPT), `failed`, `not_in_feed` (never adds a row).
  - Fail-open everywhere; `run()` never raises. Bounded concurrency, shared `analysis` rate-limit bucket as a background pipeline (leaves the interactive reserve alone), one retry on 429.
- `data_feed.py`: `DataFeedStore.replace_fields(symbol, set_fields, drop_keys)`. `put_symbol` is merge-never-wipe, which is wrong for a field GROUP (an old demand zone must not survive beside a new `ms_asof`). This drops then sets, writes canonical + alias + legacy keys, never creates a row, never touches the index.
- `main.py`:
  - `POST /data-feed/structure-refresh` (`?force=1`, `?limit=N`, `?dry_run=1`; `/api/...` alias): returns at once, work runs in a daemon thread; a lock stops two near-simultaneous triggers.
  - `GET /data-feed/structure-refresh/status`: job + selection tally + lifetime counters (`stalled` if no progress for `STRUCTURE_REFRESH_STALE_AFTER_SEC`).
  - `POST /data-feed/structure-refresh/stop`.
  - QStash tick: body `{"action":"structure-refresh"}` starts it (optional `force`, `limit`).
  - Added to the jobs snapshot broadcast and to the "job is active" check; a GET on the action name now answers 405, not the symbol catch-all.
  - Counters on `/metrics`: `stockky_structure_refresh_total{outcome}`, `stockky_sniper_structure_fetch_total{outcome=requested|attached|missed}`, `stockky_prepare_structure_total{kind=checked|no_opinion|turned_bad|flipped|with_triggers}`. All through `_structure_metric`, which never raises.

## Config (`.env.example`, `docs/ENV_REFERENCE.md` regenerated)
`STRUCTURE_REFRESH_ENABLED` (1), `_CONCURRENCY` (3), `_MAX_SYMBOLS` (0 = no cap), `_RECHECK_H` (6), `_TIMEOUT_S` (20), `_STALE_AFTER_SEC` (600).

## Schedule it
Nothing schedules itself. Add a QStash schedule (or cron) at about 16:00 IST, Mon-Fri, that POSTs `{"action":"structure-refresh"}` to `/ops/qstash/tick`, or `POST /data-feed/structure-refresh` directly. On a holiday it finds nothing newer and ends quickly (rows are stamped, so it is not retried within the recheck window).

## Tests
`tests/test_group360_structure_refresh.py` (67): expected bar date (buffer, weekend, holiday run, naive time), `ms_asof` parsing, row state / priority / recheck, selection order and limit, `refresh_one` (group replaced, old zone dropped, unchanged, no-block keeps keys, unknown schema, store errors), plan (bulk + fallback), run (counts, lifetime, stop, executor failure, stalled), fetcher (URL, 429, failures), real `DataFeedStore.replace_fields`, and `main.py` wiring checked as source / AST.
Two older tests that lift route code out of `main.py` (`test_group357_sniper_route_enrich`, `test_group358_prepare_structure_notes`) got a no-op `_structure_metric` in their namespace.

## Not run
Real service with fastapi (not installed here), a live feed store / technical service round trip, `npm run build`. The full gateway suite has the same 81 failures / 42 collection errors as the untouched group 359 zip (missing yfinance / sqlalchemy in this sandbox); +67 passes.

## Not done
No frontend button / status line for the job yet (use the endpoints). Feed-health structure coverage in the UI and the sniper outcome tracking are still open.
