# group146 (2026-10-04) - /training/api/insights no longer returns invented insights

Cumulative on group145. Rebuild decision-prediction-service: `docker compose build decision-prediction-service && docker compose up -d`.

## Cause
`services/decision-prediction-service/training/app.py::get_learning_insights` returned three hard-coded example insights (sample sizes 124 / 87 / 65, "high"/"medium" confidence, `active: true`) whenever `training_report.joblib` existed in the working directory, and a 404 otherwise. The Training tab rendered them as if they were learned from real trades. Nothing in the service computes learned insights (`insights.InsightGenerator` is not wired to any data source).

## Fix
- `get_learning_insights()` now returns `{"insights": [], "last_updated": <IST now>, "note": "No learned insights are computed yet."}` with no dependency on a report file or the insights module.
- `/api/insights` therefore answers 200 instead of 404/501. The Training tab already renders an empty list as "No insights available yet." (no frontend change).
- `HAS_INSIGHTS` and the optional `insights` import are left in place (unused by this route; harmless).

## Tests
`tests/test_learning_insights_honest.py` (4 tests): empty list, independence from report file / module flag, no hard-coded example text left in `app.py`, route returns 200 with an empty list. Training suite: 42 passed.

## Closed as keep-as-is (no code change)
- Item 9 remainder (news source): no new source; Google News already supplies items, group 108 backoff stops wasted Yahoo calls.
- Item 12 (discount scoring on one-bar technical read): stays label-only until outcome data exists.

## Still open
Item 6 (yfinance on the VM: needs `yf_report.txt` from `scripts/diagnose_yfinance.sh`); 2027 holiday dates (wait for NSE's December list, then run `scripts/check_holiday_lists_sync.py`).
