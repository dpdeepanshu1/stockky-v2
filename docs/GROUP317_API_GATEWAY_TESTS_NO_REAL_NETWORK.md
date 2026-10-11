# Group 317 - api-gateway unit tests can no longer reach the real network

Tests only; no service logic changed.

## Problem
Recording every DNS lookup / connect during the full api-gateway run showed **19 tests resolving production hostnames**:
- 18 in `test_main_scan_runner.py::TestNifty50Data`: the open-session movers fetch asks market-data `POST /quotes/bulk` first
  (group275) and the `Movers` fixture only stubbed `httpx.get`, so `httpx.post` went to `market-data-service-r6d7.onrender.com`.
- 1 in `test_main_surprise_routes.py::TestRunPremarketFeed::test_missing_scanner_module_is_a_500`: with no body the route builds the
  real scan universe first (analysis-intelligence, Google News, NSE) before it reaches the import the test makes fail.
On the VM that is a live request to a production service from a unit test; in CI it is a flaky DNS dependency.

## Fix
1. `Movers` fixture stubs `httpx.post` as "market-data unreachable" (falls through to the yfinance stub, which is what those tests describe).
2. The premarket-feed test passes a symbols body, so the universe is not built.
3. `tests/conftest.py`: autouse `_no_real_network` blocks `socket.getaddrinfo` / `socket.connect` to anything but loopback and
   **fails the test that tried**, even when the code under test swallows the exception (the usual `except Exception`).
   Deliberate live run: `ALLOW_TEST_NETWORK=1 python -m pytest ...`.
4. `tests/test_no_real_network_guard.py` (3 tests): a real hostname fails the test, loopback is allowed, the escape hatch works.

## Checked
Full api-gateway suite: **8606 passed** (8603 + 3 new), with the guard on.

## Not covered here
The same guard is not yet in the other six services' conftests (not measured). The "order-dependent weekday failure" named in
group 315 did not reproduce (Sunday clock); it needs the failing test name from you.
