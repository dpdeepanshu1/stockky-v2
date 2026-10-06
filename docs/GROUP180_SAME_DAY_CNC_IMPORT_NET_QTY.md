# Group 180 — same-day CNC import uses the net quantity (real-trade-service)

Carried-over item: "the POWERGRID order imported as a 'same-day CNC' position".

## What I could and could not establish
The log has no line about it, and the code path that imports a same-day CNC buy is deliberate (group of 2026-09-17: a
delivery buy appears in `get_positions()` before it settles into holdings, so it is imported, tagged `broker_imported`,
and sold as CNC; a same-day CNC SELL is then rejected by Dhan until T+1, which the exit engine already handles with its
CDSL-pending alert). I found nothing wrong with importing such a position per se. **If your complaint was something else
(e.g. it should not have been imported at all, or the exit behaved badly), I need the POWERGRID details: the order time,
the `import_broker_holdings: imported POWERGRID ...` log line and what the Dhan app showed.**

## The real flaw I did find and fixed (`portfolio/portfolio.py::import_broker_holdings`)
For a same-day CNC row the import used the BUY side quantity (`netBuyQty` / `buyQty`) and never looked at what was sold
today. A row with buyQty 10, sellQty 4, netQty 6 became a tracked position of 10; a row sold out today (netQty 0,
positionType CLOSED, productType still CNC) became a ghost position whose exits could only be rejected.
- Now, when Dhan reports `netQty`: net <= 0 is skipped (debug line), net < buy is imported at the net quantity (INFO line).
- A row with no `netQty`, an unparseable one, or net equal to buy is unchanged. A settled holding still wins over a
  positions row for the same symbol. `holdings_sync_reconcile` already used net quantity, so the two now agree.
- `IMPORT_CNC_CAP_TO_NET_QTY=0` restores the old buy-side quantity.

## Tests
`tests/test_portfolio.py` (+9 cases in the same-day CNC section). Sandbox: `test_portfolio.py`,
`test_portfolio_remaining_coverage.py` and the group 174 re-import-skip tests, 101 passed.

Rebuild real-trade-service.
