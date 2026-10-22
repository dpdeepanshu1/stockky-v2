# E47 - the calibration page's risk-per-trade switch now reaches the engine (2026-10-23)

## The bug (found while doing E46)
The Real Money Calibration page edits `config.DEFAULT_RISK_PER_TRADE_PCT`. The risk engine never read that: it reads the stored row `trade_risk_config.risk_per_trade_pct`, and the config value only seeds a NEW row. So the page switch (marked live) changed nothing on a real account.

## The fix
New `risk_engine/effective_risk.py` -> `effective_risk_pct(row)`:
- row `updated_by` is one of our own tags (`system-seed`, `system-migration-session79`, `system-migration-e46`): the live config value is used, read at call time, so the page works at once;
- an admin saved the row (`PUT /risk-config`, disarmed) or it is an old untagged row: the stored number wins, exactly as before.

Used at the three places that build the engine's `AccountState` (`entry_engine/entry.py`, `manual_engine.py`, `/risk-engine/check` in `main.py`) and in the two routes that show the value, so the screen and the engine agree. New rows are stamped `system-seed`; the E46 boot migration tags an old never-edited row that is still at 5.0 (and moves it to the 2.0 default).
Safe fallbacks: a bad / non-positive config value uses the stored number; a row without the field does not raise.

## What it means for you
- Change the number on the page -> applies on the next order check, while the row is system-owned.
- If you ever save a value through `PUT /risk-config`, that row is yours and the page switch no longer overrides it (the page help says so).
- An old untagged row below 5.0 (for example one you set long ago) keeps its stored number until you save or reset it.

## Tests
`tests/test_e47_effective_risk.py` (6; run here against the real module through a stand-in runner, they need no database). E46 migration tests unchanged. Not run: the full pytest suites (no pytest here); run `bash scripts/docker_test.sh`.
