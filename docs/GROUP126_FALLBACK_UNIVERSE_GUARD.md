# group126 (2026-10-04) - guard: built-in boot universe has no delisted names

Cumulative on group125. Test-only; no production code changed.

Group 125 filters api-gateway's refreshed `/scan/universe` against `KNOWN_DELISTED_SYMBOLS`, and listed the built-in 250-symbol boot universe as "not checked". It is now checked: the list (`surprise_premarket._FALLBACK_LIQUID_UNIVERSE`, 250 entries) contains none of AAKASH, ANNAPURNA or TATAMTRDVR, has no duplicates and no blanks (verified by reading the real list here).

`tests/test_fallback_universe_has_no_delisted.py` (2 tests) pins that, so adding a delisted name to the built-in list later fails the suite. The AngelOne warning "resolved 248/250" therefore refers to other symbols with no scrip-master token; the group124 warning will name them on the next boot. Tests not run under pytest here (no fastapi/pytest); the underlying checks were run standalone against the real list.
