# Group 203 - /scan/universe no longer holds callers for the full movers deadline on cold start (api-gateway)

Cumulative on group 202. Item 5 of the open list ("api-gateway cold start after a restart"). Rebuild api-gateway.

## What the 2026-10-06 boot log shows
After the restart three `GET /scan/universe` calls (two `?cached=true`) each logged
`scan/universe: momentum movers not ready within 12s - returning without them`, i.e. each caller was held for the full 12 s
(`SCAN_UNIVERSE_MOVERS_DEADLINE_S`) while the startup warm-up pass was still computing, then got the universe with no movers anyway.
The movers were only ready later (`Momentum movers collected: 115 symbols`, `momentum-movers cache pre-warmed`).
Groups 196/197 already fixed the universe half (stale copy served + real background rebuild); this is the movers half.

## Change (`api-gateway/main.py`)
- New module flag `_MOVERS_EVER_READY`, set by `_note_movers_ready()` when `_get_momentum_movers` returns a non-empty list (cache hit, single-flight
  leader, or the non-single-flight path).
- `_movers_with_deadline()` waits `min(SCAN_UNIVERSE_MOVERS_DEADLINE_S, SCAN_UNIVERSE_COLD_MOVERS_DEADLINE_S)` until that flag is set; afterwards the
  full deadline as before. `SCAN_UNIVERSE_COLD_MOVERS_DEADLINE_S` default 5; 0 = off (always the full deadline). The warning text names the deadline used.
- The computation keeps running in the background and warms the cache, exactly as before; the response is still flagged `momentum_movers_partial`.

## Trade-off (read this)
In the first seconds after a restart a caller can now get no movers after 5 s where it might have received them at 6-12 s. In the one boot log we have, they
did not arrive within 12 s for any of the three callers, so nothing observed is lost. If your VM's movers pass usually finishes in 5-12 s at boot, set
`SCAN_UNIVERSE_COLD_MOVERS_DEADLINE_S=0`. Once one pass has completed, behaviour is the old one.

## Not changed
The 90 s movers cache, single-flight (group 189), the startup warm tasks, the 20 s build deadline, `surprise/scan` (its "exceeded 20s - served last computed
result" line in the same log is the intended fallback).

## Tests
`tests/test_group203_cold_movers_deadline.py` (18 cases): deadline choice, env parsing, flag set by each path, cold caller returns early, warm caller waits,
fast result still returned, warning text. No pytest/fastapi/starlette in the sandbox: the code was extracted into a stub module and 17 of 18 cases passed under a
stand-in runner; the 18th (log text, needs pytest's `caplog`) and the existing api-gateway suites were NOT run. Run on the VM:
`cd services/api-gateway && python -m pytest tests/test_group203_cold_movers_deadline.py tests/test_main_market_universe_routes.py tests/test_group189_momentum_movers_single_flight.py -q`.
