# Session 127 — analysis-intelligence-service: event_depth utcnow fix, 100% coverage, CI gate (2026-09-29)

## Baseline (full suite, `./run_tests.sh`)
Before: 4136 stmts, 32 missed, 99% total (the 4% in the user's terminal paste came from running
only tests/test_event_depth.py, so every other module showed 0%).
After: 4144 stmts, 26 missed, 99% total (99.37%). event/event_depth.py 97% -> 100%.

## Changes
- event/event_depth.py: replaced both `datetime.utcnow()` (deprecated in 3.12) with `_utcnow()` /
  `_naive_utc()`. Deliberately stays NAIVE UTC: dates in this module come from
  `datetime.fromisoformat(str(x)[:10])` (naive); a plain `datetime.now(timezone.utc)` would make
  `now - d` raise TypeError, which the surrounding `except Exception` swallows, silently disabling
  earnings-proximity and recency decay. A caller-supplied aware `now` is now converted to naive UTC
  instead of failing.
- tests/test_event_depth.py: +8 tests (helpers, aware-`now` handling, no-DeprecationWarning check,
  and the 3 previously uncovered fallback branches: _decay bad half_life, unparseable
  next_earnings_date, non-numeric surprise_pct). 79 pass with `-W error::DeprecationWarning`.
- run_tests.sh: coverage regression gate, `--fail-under=${COV_MIN:-95}` in both modes.

## Not changed
- event/main.py, news/main.py, news/news_quality.py still call `datetime.utcnow()` (they compare
  against naive DB/feed datetimes; fix needs per-call-site review, not a blind swap).
- .coveragerc already lists every sub-app as a source root and omits only tests, so no omit of
  main.py entrypoints was added (they are now covered and should stay measured).

## Remaining misses (26 stmts)
event/main.py 430-431; fundamental/main.py 8-9, 659-661; fundamental/peer_multi_quarter.py 173-175;
fundamental/peers.py 74-75; news/main.py 11-12, 657-659; news/news_quality.py 158-159;
sentiment/main.py 37-38; technical/main.py 692-693, 765-767.
