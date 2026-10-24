# P2 item 14: two extra fields in the ORB shadow recorder - E104

Position Stocks, longs. RECORD ONLY: `would_enter`, `reasons`, limit and stop are exactly what they were; nothing is blocked, resized or ordered.

## What was added
`orders/orb_shadow.py::bar_fields()` adds to `flow_json["orb"]` (from today's 1-minute bars in `feed/bar_store`):
- **Close-based trigger.** `close_trigger` is true when the FIRST completed bar after `ORB_RANGE_END_IST` (09:20) closed ABOVE the range high. A wick through the high that closes back inside, or a close exactly on the high, is not a trigger. The existing verdict still uses the live price; this is the stricter reading to compare it with.
- **Trigger-bar volume against the range average.** When a trigger bar exists and the opening range has bars: `trigger_bar_at`, `trigger_close`, `trigger_volume`, `range_avg_volume` (average volume per bar of the 09:15 to 09:20 bars) and `volume_ratio` (trigger volume / range average). `n_range_bars` says how many range bars the average used (after a mid-session restart the bars may be missing; then there is a trigger but no ratio).
- A bar that is still forming is never used; bars before 09:15 are ignored; one unreadable bar row is skipped, not fatal. No bars at all: no new keys. The `v` field stays 1 (the fields are additive, readers of the old shape are unaffected).

Nothing new to switch: `ORB_MODE=off` stops all of it, as before.

## Proof
`tests/test_orb_bar_fields.py` (10 tests, stdlib only): trigger and ratio, wick-only, close equal to the high, forming bar, first trigger wins, pre-open bars and range-only average, no range bars, no bars, junk rows, and "the verdict fields are identical with and without bars". Six deliberate breakages (close >= high, forming bar counted, 09:20 bar counted in the range, last instead of first trigger, ratio inverted, pre-open bars counted) are each caught. Existing scripts still pass (`test_short_live` 47, `test_short_exit_paths` 61, `test_trade_odds` 16, catalog and shared-copy guards).

Not run here: pytest and SQLAlchemy (pypi blocked), so `python3 -m pytest tests -q` in position-stocks-service is for the VM (it includes the older ORB tests in `test_group479_guards.py`, which should be unaffected: with no bars nothing is added).

## Not changed
`records_report.py by_orb_shadow` does not group by the new fields yet; that is for when there are enough recorded trades to compare close-trigger against tick-trigger and by volume ratio.
