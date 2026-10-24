# E113 - Real Trade ORB bar fields and ORB groups in records_report (24 Oct 2026)

Closes open item 3 of the plan. RECORD ONLY: `would_enter`, `reasons`, limit and stop of the ORB verdict are exactly what they were. Nothing is blocked, resized or ordered.

## What was added
- **`entry_engine/orb_bars.py` (new, Real Trade).** Gives `flow_json["orb"]` the two E104 fields Position Stocks already had: `close_trigger` (first COMPLETED bar after `ORB_RANGE_END_IST` closed ABOVE the range high; a wick that closes back inside, or a close exactly on the high, is not a trigger) and, for that bar, `trigger_bar_at`, `trigger_close`, `trigger_volume`, `range_avg_volume`, `volume_ratio`, plus `n_range_bars`.
- **Where the bars come from.** Real Trade has no tick-built bar store, so `GET /history/{symbol}?period=5d&interval=1m` on market-data-service, fetched on a **background thread** and read from a cache. The entry decision never waits for the network: a symbol seen for the first time has no bars yet (no new keys); later decisions do. Max `ORB_BARS_MAX_INFLIGHT` (3) fetches at once, one per symbol per `ORB_BARS_REFRESH_S` (60 s).
- **Honesty rule.** If the newest cached bar is older than `ORB_BARS_MAX_LAG_S` (180 s), no fields are written (a stale cache would wrongly say "no close trigger"). Status route `orb.bars` shows `hit / miss / stale / requests / ok / failed`.
- **Records.** `trade_records._orb_fields` adds `orb_close_trigger` (only a real boolean counts) and `orb_volume_ratio`. Trade report gains `by_orb_close_trigger` and `by_orb_volume_ratio` (same buckets as Position Stocks). Old trades or trades without bars are `no_orb_record` / `none`, never "no trigger".
- **`scripts/records_report.py`** gets three groups: ORB verdict (group474, grouped for the first time), close-based trigger, trigger-bar volume vs range average.

## Switches
`ORB_MODE=off` stops everything. `ORB_BARS_FETCH=false` (env) stops only the fetching. Env tunables: `ORB_BARS_REFRESH_S`, `ORB_BARS_MAX_LAG_S`, `ORB_BARS_TIMEOUT_S`, `ORB_BARS_MAX_INFLIGHT`.

## Proof
- `tests/test_e113_orb_bars.py` (15 tests, stdlib only, pytest-compatible).
- 12 deliberate breakages, all caught (run with bytecode off; an earlier stale `.pyc` had hidden two same-length mutants).
- `test_group474_orb_records.py` compared an exact dict; it now expects the two new keys (edited on purpose). `tests/conftest.py` gets an autouse fixture that turns the bar fetch off in all other tests.
- Guards green: check_control_catalog, check_shared_copies, check_merge_conflicts, check_secrets, check_requirements_drift, check_market_data_url_default, check_price_source_imports.

## Not run here (no pytest / SQLAlchemy / fastapi in this workspace)
- Whole real-trade pytest suite and the tmp_path tests of `scripts/test_records_report.py`: run on the VM.
- **How fresh market-data's 1-minute history is at 09:21-10:30 is unknown.** If it lags over 3 minutes most decisions count as `stale` and carry no fields; `orb.bars` will show it after Monday 26 Oct. Raise `ORB_BARS_MAX_LAG_S` only if you accept a trigger that may be minutes old.
