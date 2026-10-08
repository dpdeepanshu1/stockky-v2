# Group 257 - two misleading log lines (2026-10-08 ~09:40 IST log, after group 256)

## 1. "AngelOne-first did not price X (angelone_quote cooldown) - using the Yahoo path" (market-data-service main.py)
Since group 256, unheld symbols do NOT reach Yahoo during an AngelOne quote cooldown: they get a cached price no older than
`QUOTE_COOLDOWN_STALE_MAX_AGE_S` (default 180 s), or no price. The line still said "using the Yahoo path".
`_ao_first_miss` now builds its ending with `_ao_first_miss_tail(reason)`:
- cooldown miss (default): `... (angelone_quote cooldown) - serving a cached price <= 180s old, else no price (Yahoo skipped)`
- `QUOTE_COOLDOWN_SKIP_YAHOO=0`: ends with `else the Yahoo path`
- `QUOTE_COOLDOWN_SERVE_STALE=0`: old text `using the Yahoo path`
- every other miss reason: old text. Log only; no behaviour change. Blank env values are safe.

## 2. "entry_engine: REAL regime WEAK score=0 < gate=33 ... - BUYs blocked." (real-trade-service entry_engine/entry.py)
In the log a BUY order went out (`place_order ... BUY ... qty 9 LIMIT CNC 290.35`) right after this line. Checked in code: not a gate leak.
`ENTRY_REGIME_OVERRIDE_TOP_N` (default 1) lets the single highest-conviction actionable candidate through a WEAK regime at
`ENTRY_REGIME_OVERRIDE_RISK_SCALE` (default 0.5) of normal risk; every other candidate gets the Gate 3 WAIT. The line now says so:
`... - BUYs blocked (except the top 1 conviction candidate(s), let through at 50% risk).`
With `ENTRY_REGIME_OVERRIDE_TOP_N=0` the old ending is kept. Log only.
To stop that BUY entirely, set `ENTRY_REGIME_OVERRIDE_TOP_N=0` in the real-trade-service env (your call; not changed here).

## Not changed
No limits, gates, cooldown values or lane budgets.

## Tests
`market-data-service/tests/test_group257_cooldown_miss_log_text.py` (5), `real-trade-service/tests/test_group257_regime_weak_log_override_note.py` (2).
Group 256/257 market-data tests: 37 passed. Full market-data run: 16 failures, identical list before and after this change
(test-order pollution in my sandbox, each group 256 file passes alone).
