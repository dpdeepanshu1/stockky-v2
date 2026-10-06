# Group 189 - one momentum-movers pass at a time

Cumulative on group 188b. Item 12 of the 2026-10-06 boot-log list. Rebuild api-gateway.

## What the log showed
The NSE gainers, losers, volume-gainers, NIFTY 500 and `momentum_movers step1` sequence ran twice at the same moment.
`_get_momentum_movers` has a 90 s result cache, but the cache is only filled when a pass FINISHES. Two callers that
arrive while it is still empty (the startup pre-warm and the first `/scan/universe`, for example) each ran a full pass:
4 NSE board fetches, the AngelOne whole-market sweep and the bulk yfinance fallback.

## Change (`api-gateway/main.py`)
- `_get_momentum_movers()` is now a thin wrapper; the old body is `_compute_momentum_movers()`, unchanged.
- The first caller computes; callers that arrive meanwhile wait for its answer (each gets its own copy of the list).
  A follower that waits longer than `MOMENTUM_MOVERS_JOIN_WAIT_S` (default 45) or finds the leader failed computes for
  itself, exactly as before, so a slow or broken pass can never block everyone. A call from the computing thread itself
  never waits on itself. A pass that returns a non-list is not shared.
- `MOMENTUM_MOVERS_SINGLE_FLIGHT=0` restores one pass per caller.
- The wrapper keeps the public name, so the 11 call sites and the tests that patch `_get_momentum_movers` are untouched.

## Not changed
Why one pass is slow (NSE boards 403, the 2707-symbol sweep) is the load work of items 7, 8 and 14. This only stops the
same slow work running twice. It does not cover two separate gateway processes (per-process lock, like group 175's).

## Tests
New `tests/test_group189_momentum_movers_single_flight.py` (10 cases, with real threads): shared pass, own copies,
next call recomputes, cache hit, leader failure, follower timeout, switch off, re-entrancy, non-list, bad env value.
Sandbox: the five existing files that use `_get_momentum_movers` pass (959); full api-gateway suite 8296 passed and
6 failed, the same 6 order-dependent `test_searched_list_selfclean.py` cases that fail on the uploaded zip.
