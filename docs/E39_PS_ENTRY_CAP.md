# E39 - Position Stocks real daily entry cap (2026-10-23)

Audit finding: Position Stocks' only daily limit was `POSITION_STOCKS_DAILY_ORDER_BUDGET` (300), practically no cap. Real Trade has `RISK_MAX_ENTRIES_PER_DAY` = 6.

- New `POSITION_STOCKS_MAX_ENTRIES_PER_DAY` (config `MAX_ENTRIES_PER_DAY`), ships at **15**, 0 = off. On the calibration page under Risk caps (call-time read, `check_control_catalog.py` passes).
- `orders/entry_cap.py`, checked in `attempt_entry` right before the max-open-positions check. Skip reason `ENTRY_CAP:...`.
- Counted from `scalp_positions` (opened today IST, status ERROR not counted), so a restart does not reset it. Fails open.
- The number 15 is a placeholder decision: change it on the page (or the env var) once you pick one.
- Tests: `tests/test_e39_entry_cap.py`. The DB-counting test needs sqlalchemy (not available in the sandbox); run pytest in CI.
