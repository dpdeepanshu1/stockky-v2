# E119: the opening-range recorder now mirrors for a short (plan P3: "close below range low")

Before: the ORB recorder only knew the long side. A short position carried no ORB record, so the report groups `by_orb_close_trigger` / `by_orb_volume_ratio` could not say anything about shorts, and the only way to read an ORB record on a short would have been to apply a long verdict to it.
Now: `orders/orb_shadow.py` takes `side`. For `SHORT` (live shorts only):
- verdict: refuses with `above_range_low` / `above_vwap` (mirror of `below_range_high` / `below_vwap`); spread and range-width rules are unchanged;
- `limit_price` = range low x (1 - ORB_LIMIT_BUFFER_PCT %), `stop_price` = range high, `stop_pct` measured up to the range high;
- close-based trigger: first completed bar after ORB_RANGE_END_IST that closes BELOW the range low (a wick below that closes back inside is not a trigger); trigger-bar volume against the opening-range average, as for longs;
- the record carries `"side": "SHORT"`. A long record is byte-for-byte what it was (no `side` key).
`orders/short_live.py` attaches it to the live short's `flow_json["orb"]` (fail-safe: any error is logged and ignored, the entry is unaffected). The shadow-only short rows (`short_entry.py`) are not touched (they have no flow record).

RECORD ONLY: nothing is ordered, blocked or resized. No new switch; `ORB_MODE=off` still stops all of it. Report groups use the same keys, and `by_side` (E110) separates longs from shorts, so read `by_orb_close_trigger` together with it.
Not done: a bulk whole-market scan (the plan's other half of this line). It would need a data source for many symbols and is still low priority.

Checked here (pytest and SQLAlchemy are not installed in this sandbox, stand-in runner): new `test_e119_orb_short_mirror.py` 8/8, `test_orb_bar_fields.py` 10/10, `test_group479_guards.py` 16/16, `test_short_live.py` 10/10, catalog coverage 3/3. Mutation check: with the short trigger reverted to the long rule, 3 of the new tests fail. NOT run here: `test_e110_report_side_and_orb_groups.py` (needs SQLAlchemy); run the real suites on the VM (`stockky_full_test.sh`).
