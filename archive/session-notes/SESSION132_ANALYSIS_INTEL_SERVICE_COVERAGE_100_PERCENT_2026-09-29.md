# Session 132 — analysis-intelligence-service: 100% line coverage (2026-09-29)

Full suite: 4150 stmts, 0 missed, 100.00%. 1786 tests pass (`bash run_tests.sh` and `--single`).
Tests only; no production code changed this pass.

- fundamental/peer_multi_quarter.py 173-175: a worker exception escaping fetch_fundamentals inside the
  thread pool is logged and mapped to {} while other symbols survive (monkeypatched fetch_fundamentals).
- sentiment/main.py 37-38: import survives yfinance builds with neither `set_session` nor `shared`
  (runpy with stub yfinance modules; also covers the shared-only variant).
- technical/main.py 692-693: regime-penalty `except` — resistance value whose `>` raises is swallowed and
  the penalty skipped (float subclass with a raising __gt__).
- technical/main.py 765-767: `__main__` uvicorn block (default port 8002), fake-uvicorn test.

Notes
- tests/test_sentiment_main.py cannot run under `-W error::DeprecationWarning`: starlette's TestClient
  triggers an anyio alias DeprecationWarning at import (third-party, unrelated to this repo's code).
- Gate: run_tests.sh defaults to --fail-under=95. Now that the service is at 100%, use
  `COV_MIN=100 bash run_tests.sh --single` to lock it in.
- Next target: api-gateway (2%, 14,289 stmts; only qstash_client.py tested).
