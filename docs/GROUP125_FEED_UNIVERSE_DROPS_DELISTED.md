# group125 (2026-10-04) - feed universe no longer subscribes known-delisted symbols

Cumulative on group124. Run `bash run_tests.sh` in market-data-service on the VM.

## Why
Group 124 made the AngelOne feed's "resolved N/M" warning name the symbols with no token. That was reporting only. Known-delisted names (AAKASH, ANNAPURNA, TATAMTRDVR) could still arrive in api-gateway's `/scan/universe` and be handed to both WS feeds, where they can never produce a tick.

## Change (`market-data-service/main.py`)
- New `_clean_feed_universe(raw)`: strips `.NS`/`.BO`, upper-cases, keeps order, skips blanks, and drops anything in `KNOWN_DELISTED_SYMBOLS`. Returns `(symbols, dropped_sorted)`; non-list input gives `([], [])`.
- `_refresh_feed_universe_loop` uses it. When the dropped set changes it logs once: `feed universe refresh: dropped N known-delisted symbol(s) from the feed: AAKASH, ANNAPURNA` (not on every 15-minute refresh).
- An empty result still leaves the running feed untouched (existing behaviour). Nothing else about the refresh changed.

## Not changed
The built-in 250-symbol boot universe (not checked against the delisted list here), and symbols that are dead but not yet in `KNOWN_DELISTED_SYMBOLS`; the group124 warning will name those.

## Tests
`tests/test_feed_universe_drops_delisted.py` (5 tests). Sandbox has no fastapi/pytest: the real helper and delisted set were run standalone (5/5 pass); the test file compiles, not run under pytest.
