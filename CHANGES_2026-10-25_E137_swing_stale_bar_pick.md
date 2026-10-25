# E137 - swing lane: a stock with a stale last bar is never picked (2026-10-25)

**Bug (reading `swing/engine.py` and `ranker.py`):** the snapshot's bar date is the newest bar of ANY stock. A stock that is halted, suspended or has stale data keeps its old last bar (days old) but still gets a rank, a `screen` flag and an ATR. It could be PLANNED as a pick. In shadow it was then SKIPPED at settle ("no bar for the entry session"), wasting one of the day's pick slots; in `live` the morning entry reads the PLANNED rows and would try to buy it on the strength of old prices.

**Fix:** `lane.pick_candidates(rows, exclude, n, asof=None)` drops any row whose `last_bar_day` is not `asof`; `engine.plan_shadow` passes the scan's bar date. `asof=None` keeps the old behaviour for any other caller. Nothing else changes (the stale stock is still ranked, so percentiles are as before; it just cannot be picked).

Checked here: `tests/test_swing_lane.py` 30 pass (1 new: a rank-1 stock with a 6-day-old bar is not planned, the next one is; no `asof` = old behaviour); removing the check is caught. Momentum tests and guards pass.
Not run: pytest, SQLAlchemy / FastAPI paths (unchanged; this edit is in pure code the tests do run).
