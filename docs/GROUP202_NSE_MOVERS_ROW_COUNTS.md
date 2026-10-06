# Group 202 - NSE movers log line says how many rows each board returned (api-gateway)

Cumulative on group 201. Item 4 of the open list ("NSE endpoints degraded"). Rebuild api-gateway (log text only).

## What the repo and the 2026-10-06 boot log show
- The degradation is the NSE/Akamai block of this VM's datacenter IP: `bootstrap cookies weak (AKA_A2; status 403)`, `Fetched 0 securities from NSE`.
  Code cannot get past it. Group 178 already pauses all NSE API calls for 10 min after a surviving 401/403/429 and serves stale cache;
  group 195 documented the weak-cookie line as informational; item 22 (2026-10-04) already separates "unreachable" from "200 with 0 rows" for
  the securities list. Nothing in code was left to fix for the block itself.
- What the log still could not say: the movers boards print `+0 symbols` for both "NSE returned nothing" and "NSE returned a full board of
  stocks all moving under 2%". In the boot log `gainers +11`, `losers +0`, `volume-gainers +0`, `NIFTY 500 +96` - the two `+0` boards could be
  either; the log did not tell.

## Change (`api-gateway/main.py::_get_momentum_movers`, step 1)
The per-board line is now
`NSE movers <endpoint>: +N symbols (rows=R, under 2% move=Q)`
- `rows` = dict rows the board returned; `under 2% move` = rows with a usable symbol and a real move below 2% (valid, just not a mover).
- `rows=0` with `no data` warning above = blocked/unreachable; `rows=0` without it = NSE answered 200 with an empty board;
  `rows>0, +0` = a quiet board. Behaviour (which symbols are added, caching, pauses) is unchanged.

## Not changed
The NSE block itself (needs a different egress IP or a browser-grade TLS client, your side), the 2% threshold, the pause logic.

## Tests
6 new tests at the end of `tests/test_main_universe.py` (`test_group202_*`): mixed board, empty board, no-data board, non-dict rows, unusable
symbols, result unchanged. No pytest/fastapi in the sandbox, so the suite was NOT run; the edited loop was run in isolation with stubs and printed the
expected lines. Run on the VM: `cd services/api-gateway && python -m pytest tests/test_main_universe.py -q`.

## After you rebuild api-gateway
`docker compose logs --since 15m api-gateway | grep "NSE movers"` and paste the lines.
