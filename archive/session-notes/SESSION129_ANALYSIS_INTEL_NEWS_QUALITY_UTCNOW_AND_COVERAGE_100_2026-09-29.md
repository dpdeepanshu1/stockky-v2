# Session 129 — analysis-intelligence-service: news/news_quality.py to 100% + utcnow removal (2026-09-29)

- news/news_quality.py 98% -> 100%. Total 4148 stmts, 22 missed.
- `datetime.utcnow()` -> `_utcnow()` (naive UTC, same value; compared against naive feed timestamps).
- Lines 158-159 (`_ts` sort-key except): reachable when an item's `.get("published_at")` raises after
  `.get("title")` succeeded (dedupe runs first). Added test_sort_key_failure_is_tolerated (dict subclass).
- tests/test_news_quality.py uses `nq._utcnow()`; 43 tests pass under `-W error::DeprecationWarning`.
- Remaining misses (22): fundamental/main.py 8-9, 659-661; peer_multi_quarter.py 173-175; peers.py 74-75;
  news/main.py 11-12, 657-659; sentiment/main.py 37-38; technical/main.py 692-693, 765-767.
- Still on utcnow(): news/main.py (242, 426).
