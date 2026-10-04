# group98 (2026-10-04) - item 27: `SURPRISE_UNIVERSE` / `SCAN_UNIVERSE` unset -> 250-symbol fallback warning

Cumulative on group97. In `services/market-data-service` run `python3 -m pytest tests -q`, then `docker compose build market-data-service && docker compose up -d`.

## What the item turned out to be
Leaving both env vars unset is a supported setup, not a fault. `surprise_premarket.default_universe_from_env()` then returns a built-in list of 250 liquid NSE symbols as the BOOT-TIME universe for the AngelOne and Yahoo live feeds. `main.py::_refresh_feed_universe_loop` re-points both feeds at api-gateway's `/scan/universe` about 20 seconds after boot (`FEED_UNIVERSE_INITIAL_DELAY_S`), retrying every 60 s until it succeeds, then every 15 minutes. So the 250-symbol list is only live for the first seconds after a start.

The log line was wrong and noisy: it said "live universe build returned nothing", but this function never attempts a live build, and it was printed by every caller (AngelOne feed startup, Yahoo feed startup, and each `/surprise/premarket/run` call without symbols).

## Fix (`surprise_premarket.py`)
- The warning is logged once per process, and says what is happening: the built-in N-symbol universe is used until the live `/scan/universe` refresh replaces it (about 20 s after boot).
- Return value is unchanged (a fresh copy of the same 250 symbols each call). If either env var is set, nothing is logged, as before.

## Deliberately not done
- **Not set in `docker-compose.yml`.** A fixed list would replace a live, movers/news-driven universe with a stale one. Set `SURPRISE_UNIVERSE` only if you want the boot-time universe, and the premarket baselines run without symbols, to be a list you choose.
- **Premarket baselines with no symbols still use the 250 built-in names**, not the live universe: `/surprise/premarket/run` without `symbols` calls this function directly. If the baselines should cover the live universe, that is a separate change (the route would fetch `/scan/universe` first). Not changed here.
- **Not seen live:** I have not seen your VM's log, so I cannot confirm this warning is the line you meant.

## Tests
`tests/test_surprise_premarket.py::TestFallbackUniverseLogsOnce` (4): one warning across several calls, accurate wording, every call still returns a full copy, env set never warns. The autouse fixture resets the once-flag. Run here: full `market-data-service` suite 677 passed.
