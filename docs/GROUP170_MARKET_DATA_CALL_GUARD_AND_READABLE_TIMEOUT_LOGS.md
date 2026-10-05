# Group 170 - market-data overload: guarded calls from analysis-intelligence, readable timeout logs

Cumulative on group 169. Item 2 of the second open-market log list (market-data overloaded, ReadTimeouts in
quality-gate and analysis, failures logged with an empty message). Rebuild analysis-intelligence-service and
real-trade-service.

## What was wrong
- The quality gate (position-stocks-service) asks analysis-intelligence for several symbols at once every cycle.
  Each `/analyze` then calls market-data (`/fundamentals` 60 s timeout, `/history` 35 s, `/quote` 10-12 s) with
  no limit and no sharing. When market-data was slow, every extra call just waited out its whole timeout, piled
  more work on it, and ended as a ReadTimeout.
- httpx timeouts have an empty `str()`, so some of those failures were logged as nothing at all:
  `market-data history error X period=6mo: ` (technical), `Unexpected error: ` (fundamental, no symbol either) and
  `dynamic_universe: /check trigger failed ()` (real-trade-service). The quality gate itself already named the type.

## The fix
New `analysis-intelligence-service/md_guard.py`, used by `technical/main.py` (`/history`, both `/quote` calls) and
`fundamental/main.py` (`/fundamentals`) in place of a bare `httpx.get`. Nothing is cached, so nothing can go stale.
- **Concurrency cap:** at most `MD_MAX_CONCURRENT` calls in flight (default 12; 0 = unlimited). A call that gets no
  slot within `MD_SLOT_WAIT_S` (default 30) fails at once.
- **Single flight:** identical requests in flight at the same moment (same URL and params) share ONE upstream call
  and one result (or error).
- **Short cool-down:** after `MD_BREAKER_THRESHOLD` (default 8) timeouts in a row, calls fail immediately for
  `MD_BREAKER_COOLDOWN_S` (default 15) instead of each waiting out its own timeout. Any HTTP response resets the
  streak. One WARNING when it opens. `MD_BREAKER=0` turns it off.
- `MD_GUARD=0` turns the whole module into a plain `httpx.get`.
- Failures raise `MarketDataUnavailable` (an `httpx.TransportError`). `/analyze` treats it like any other
  market-data failure: fundamentals fall back to the existing IndianAPI/neutral path, technical history to the
  existing yfinance/bhavcopy fallbacks, and the quality gate to its cached signal.
- **Logs:** the three lines above now name the exception type (`ReadTimeout`, `RuntimeError: ...`), and the
  fundamental ones include the symbol. A ConnectError on `/fundamentals` is now a WARNING with the symbol instead
  of an ERROR without one; the fallback it takes is unchanged.

## What this does not do
- It does not make market-data faster. It stops analysis-intelligence from adding load while market-data is
  already struggling. The other callers (real-trade-service `/live-quote` + `/quote` for held symbols, 224
  per-symbol lookups after the bulk call) are items 4 and 9 of the open list.
- The cool-down is per analysis-intelligence process; the three sub-apps share it (same process, same module).
- During a cool-down, `/analyze` answers from fallbacks for up to 15 s. The quality gate already tolerates that
  (cache fallback), but a candidate may be gated on slightly older scores in that window.

## Tests
New `tests/test_md_guard.py` (30 tests). `test_fundamental_main.py` and `test_technical_main.py` get a
`TestMdGuardWiring` class each (6 and 5 tests). `tests/conftest.py` resets the guard before and after every test.
`test_watchlist_dynamic_universe.py` +2 tests (real-trade-service).
Not run here: the sandbox has no pytest or httpx. The guard's real code was run against a stand-in `httpx`: single
flight (6 callers, 1 upstream call), cap (12 callers, never more than 3 at once), no-slot fast failure, cool-down
open/close with the upstream untouched, shared leader error, unhashable params - all behaved as expected.
Coverage: `run_tests.sh` gates total coverage at 95%; `md_guard.py` is meant to be near 100% from its own tests.

    cd services/analysis-intelligence-service && bash run_tests.sh
    python3 -m pytest services/real-trade-service/tests/test_watchlist_dynamic_universe.py -q
