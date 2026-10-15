# Group 378 - Group B and C follow-ups

## B1 - Oracle latency endpoint in every slow-write service
`GET /ops/db-latency` now exists in real-trade-service (port 8005) and market-data-service, same output as the api-gateway one
(`oracle_compat.latency_snapshot()`: per-engine connect / exec count, avg, p50, p95, max, last slow statement, pool state, keep-warm).
Use it to see where the 2-8 s audit / live-quote writes happen: `curl -s localhost:8005/ops/db-latency`.

## B2 - News feed health (evidence only)
`GET /news/feed-health` (analysis service): per feed `ok | fallback | frozen | blocked | empty`, HTTP status, newest item age, who served it,
checks / failures. Filled as feeds are downloaded (call `/news/analyze/<SYMBOL>` once after a boot). The news score does not read it, and no
source was added or removed. Decide on sources after reading it on the VM.

## B3 - Rate limits / NSE live API
No code change (self-recovering).

## C1 - Observation lineage (asof_date / params_hash)
Traced engine -> summary -> features -> observation payload with the real engine: both fields are populated on current code, so the null INFY row
came from an older build. Added: a later decision of the day with no lineage no longer blanks stored lineage on a pending row
(`training/structure_obs.py`), and `hash` is kept in the training feature snapshot.

## C2 - Why only 1 observation
The bulk scan gets structure only from the feed rows' `ms_*` keys; those are filled by the structure refresh (0 runs). A decision without a usable
structure is deliberately not logged. New `GET /structure/observation-stats` (decision service) shows considered / built / each skip reason /
queued / posted ok / failed / cache hits.

## C3 - Structure refresh catch-up
The refresh used to start only from outside (QStash tick / GitHub workflow). The gateway now also checks every
`STRUCTURE_REFRESH_CATCHUP_INTERVAL_S` (default 1800, floor 60) after `STRUCTURE_REFRESH_CATCHUP_DELAY_S` (default 180) and starts the normal
refresh only when the market phase is `closed` / `holiday` and no run is in progress. Selection is unchanged (missing / stale rows only, 6 h
recheck), so it is a no-op once the feed is current. `STRUCTURE_REFRESH_CATCHUP=0` turns it off. Status: `/data-feed/structure-refresh/status`
now includes a `catchup` block.

## Not verified here
No pytest in the sandbox: the new decision / training / endpoint tests ran through a plain runner (56 passed); the news feed-health tests ran with
stubbed feedparser / httpx and a pytest stand-in (7 passed); the gateway catch-up helpers were checked directly. Run the service suites on the VM.

## D1 - hermetic analyze harness
`tests/_analyze_harness.py` now replaces `httpx` inside technical/main.py with an offline stand-in (Client / get / post raise). `analyze()` fetches
delivery % through `httpx.Client` to `MARKET_DATA_URL` (default: a public Render host), which the harness did not stub, so on a machine with internet
the 2 `test_group368_analyze_baseline` failures appeared (`delivery_pct 50.0 / fallback_neutral`). Reproduced with a fake internet-connected httpx:
old harness -> 50.0, new harness -> None. New test `test_harness_is_hermetic_about_delivery`.

## G1 - shadow leftovers (corrected)
`StructureGateCard.tsx` and `structureView.ts` are NOT dead: real-trade, position-stocks, the scan panel and the decision card import them, so
they were not deleted (the earlier "all dead UI" note was wrong). The record-only shadow mode is gone from the backend (decision policy: group370;
real-trade gate: none), so the remaining shadow branches only label rows stored earlier and are kept. Fixed the comments that still said shadow
is the default (`api.ts`, `StructureGateCard.tsx`, `real-trade-service/models.py`). Comment-only: the frontend build was not run here (no npm).
