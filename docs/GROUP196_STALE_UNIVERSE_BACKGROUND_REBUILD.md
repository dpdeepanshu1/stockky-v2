# Group 196 - a stale-served scan universe now gets a real background rebuild (api-gateway)

Cumulative on group 195. Item 2 of the remaining list ("slow first scan after a restart"). Rebuild api-gateway.

## What was wrong
`main.py::_build_scan_universe()` has a cold-start shortcut (added 2026-09-18): when the live key `stockky:scan_universe` is cold it serves the
durable stale copy `stockky:scan_universe:stale_fallback` at once, so the first caller after a restart does not pay the 20-30 s rebuild (which
tripped real-trade's 25 s client timeout). The comment said "background rebuild will follow" and mentioned a `SCAN_UNIVERSE_STALE_SERVED` flag.
Neither exists: the flag is never set and nothing started a rebuild. Every path that was meant to rebuild called the same function:

- the startup warm (`_warm_momentum_movers_cache`) called `_build_scan_universe()`, found the stale copy and returned it;
- `GET /scan/universe?cached=true` served the stale copy, re-wrote the live key (300 s TTL) and then ran `_build_scan_universe()` in a task - which
  hit the live key it had just written and returned at once;
- when the 300 s key lapsed the next caller got the stale copy again.

Only a real build rewrites the stale key, `kv_get_stale` ignores TTL, and the in-process copy outlives 4 h in Neon. So once one stale copy existed the
universe was re-served and never rebuilt: after a restart the first scan cycles ran on the previous session's movers / news / 52-week names, and
the fresh sources (NSE boards, AngelOne sweep, bulk deals, news) only entered the universe when no stale copy was left.

## Fix
- `_build_scan_universe_fresh()`: a real rebuild that skips both the live-cache and the stale-copy shortcuts. Single-flight through
  `_UNIVERSE_REFRESH_LOCK` (returns `None` at once if another real rebuild is running). The mode is a thread-local flag, so
  `_build_scan_universe()` keeps its signature.
- `_schedule_scan_universe_refresh()`: starts that rebuild on a daemon thread. `_build_scan_universe()` calls it whenever it serves the stale copy.
  `SCAN_UNIVERSE_STALE_REFRESH=0` restores the old serve-only behaviour (blank values keep it on).
- `GET /scan/universe?cached=true` (stale branch) now schedules `_build_scan_universe_fresh`, not the plain function.
- The startup warm rebuilds for real when the live key is cold; when the live key is already warm (a quick restart) it keeps the cheap path.
- A real rebuild that produces fewer than `SCAN_UNIVERSE_MIN_REBUILD_SYMBOLS` (50) symbols (NSE blocked, sources empty) does not overwrite the
  live or stale key; it logs `background rebuild produced only N symbol(s) - kept the stored universe`.

The request path stays fast: the caller still gets the stale copy immediately; the rebuild runs beside it and the next caller after the 300 s
live key (or the next restart) sees the fresh universe.

## Not changed
- `force_refresh=true` on the full-scan route still goes `_drop_cache_keys(SCAN_UNIVERSE_KEY)` then `_build_scan_universe()`, so with a stale copy present it
  re-serves that copy (and now schedules the real rebuild). Left alone to keep this change to the cold-start path.
- The first-ever build on a brand-new deployment (no stale copy at all) is still synchronous, bounded by `SCAN_UNIVERSE_BUILD_DEADLINE_S`.
- `surprise_scanner`'s first scan after a restart (groups 120/132/137/138 already handle that one).

## Tests
`services/api-gateway/tests/test_group196_stale_universe_refresh.py` (24 cases): stale served then really replaced, the old re-serve-forever
behaviour pinned, fresh ignores warm live/stale, single flight, lock/flag released after a crash, thin rebuild keeps the stored universe, scheduler
thread / off-switch / blank env / failing thread / thread cannot start, startup warm (cold and warm live key), cached=true route. `tests/test_main_universe.py`'s
`ub` fixture stubs the scheduler (one assertion added to the stale-serve test). Not live-tested.
