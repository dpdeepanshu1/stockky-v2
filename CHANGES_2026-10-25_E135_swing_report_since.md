# E135 - swing report: count only picks signalled since a date (2026-10-25)

Follow-up to E134. When the stock list changes (built-in -> `SWING_UNIVERSE_SYMBOLS`), the shadow ledger mixes two universes. `GET /swing/report?since=YYYY-MM-DD` now counts only trades whose signal day is on or after that date (both the closed-trade verdict and `by_status`); the answer echoes `since`. A bad date returns an `error`. Without `since` nothing changes.

Use: read `scan_universe` on `/swing/status`, note the day it changed, then `/swing/report?since=<that day>`. The verdict still needs its own minimum (30 trades, 20 days), so after a switch expect NOT_ENOUGH_DATA for weeks.

Checked here: `tests/test_swing_lane.py` 28 pass (1 new), momentum tests pass, guards pass. Not run: pytest, SQLAlchemy / FastAPI paths (the route edit is a one-argument pass-through, parsed and source-checked only).
