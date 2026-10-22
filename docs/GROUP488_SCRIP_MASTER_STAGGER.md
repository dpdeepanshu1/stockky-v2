# group488 - scrip-master: staggered refresh after boot

## What was already in place
The compose file already keeps one parsed scrip-master snapshot per service on a named volume (`scrip-cache-md`,
`scrip-cache-ps`, paths set through `ANGELONE_SCRIP_MASTER_CACHE_PATH`), so a container recreate warm-starts from disk.
The code default is still `/tmp/...`; that only matters when a service is run outside this compose file.
(I said earlier that no volume existed; that was wrong for this build.) The volumes only help from the second start on:
the very first start after deploying them has no snapshot yet.

## Change (services/position-stocks-service/feed/scrip_master.py)
When position-stocks restarts with a stored snapshot that is older than `ANGELONE_SCRIP_MASTER_REFRESH_S` (24 h), its first
background refresh now waits `ANGELONE_SCRIP_MASTER_BG_START_DELAY_S` (default 30 s, plus up to 50 % random jitter, counted
from process start). market-data-service refreshes right at boot, so the two no longer download the file at the same moment.
The stale map keeps being served meanwhile. Only the first refresh of the process waits.

Not delayed: a cold start with no map at all (delaying it would leave the feed without symbols). With the volumes in place
that case is only the first start after deploy.

## Switch
`ANGELONE_SCRIP_MASTER_BG_START_DELAY_S` (default 30, 0 = off, 0-600, blank-safe), set in docker-compose.yml for position-stocks-service.

## Tests
`services/position-stocks-service/tests/test_group488_scrip_master_stagger.py` (8). Existing scrip-master tests unchanged.
