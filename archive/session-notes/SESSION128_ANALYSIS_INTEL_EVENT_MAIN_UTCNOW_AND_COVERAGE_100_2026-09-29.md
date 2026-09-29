# Session 128 — analysis-intelligence-service: event/main.py to 100% + utcnow removal (2026-09-29)

- event/main.py 99% -> 100% (778 stmts, 0 missed). Total 4146 stmts, 24 missed, 99%.
- Replaced all 11 `datetime.utcnow()` calls with `_utcnow()` (naive UTC, identical value, zero behaviour
  change; stays naive because it is compared to naive datetimes parsed from yfinance/RSS/ISO strings).
- Lines 430-431 (bulk-deal sample `except`): reachable when `bulk_deals` is a truthy non-list (e.g. a dict),
  where `bulk[0]` raises KeyError. Added test_bulk_unindexable_truthy_value_is_tolerated.
- tests/test_event_main.py now uses `em._utcnow()` too (7 sites), so the file runs clean under
  `-W error::DeprecationWarning`. 266 tests pass.
- Remaining misses (24): fundamental/main.py 8-9, 659-661; peer_multi_quarter.py 173-175; peers.py 74-75;
  news/main.py 11-12, 657-659; news_quality.py 158-159; sentiment/main.py 37-38; technical/main.py 692-693, 765-767.
- Still on utcnow(): news/main.py (242, 426), news/news_quality.py (100).
