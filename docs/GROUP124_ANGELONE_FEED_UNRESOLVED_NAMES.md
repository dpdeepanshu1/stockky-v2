# group124 (2026-10-04) - AngelOne feed warning now names the unresolved symbols

Cumulative on group123. Run `bash run_tests.sh` in market-data-service on the VM.

## What the log showed
Every boot: `AngelOne feed: resolved 248/250 requested symbols to tokens (...)` and `resolved 495/500 ...`, with no way to tell which 2 and 5 symbols had no token.

## Change (`market-data-service/angelone_ws_feed.py`)
The warning now ends with `; unresolved: AAKASH, ANNAPURNA` (sorted, at most 20 names, then `(+N more)`). Names are normalised the same way the scrip master does (upper-case, `.NS`/`.BO` stripped). The helper `_unresolved_suffix` never raises; on any problem the warning is the old text. Which symbols get ticks, and the Yahoo fallback for the rest, are unchanged.

## Why it matters
Unresolved names are normally delisted or renamed symbols (AAKASH and ANNAPURNA already 404 on `/quote`; groups 99 and 118 drop them elsewhere). Seeing the names tells you whether the feed universe still carries any, or whether a real liquid symbol lost its token after a scrip-master change.

## Tests
`tests/test_angelone_feed_unresolved_names.py` (5 tests). Run standalone here against the real helper: pass; not run under pytest. No existing test asserted the old warning text.

## Not changed
The feed-universe builder is not filtered by this; it only reports. Dropping known-delisted names from the feed universe is a separate change.
