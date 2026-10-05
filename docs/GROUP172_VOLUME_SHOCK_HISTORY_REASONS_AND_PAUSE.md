# Group 172 - volume_shock: say why daily history is missing, stop re-asking for symbols that have none

Cumulative on group 171. Item 5 of the open list ("16 of 64 symbols have no daily history and are logged as
rejections"). Rebuild real-trade-service. Only `candidate_engine/candidates.py` changed (plus tests).

## Item 11 (held-symbol callers) - no change
`main.py:1597` is only a fallback after the display-price cache fails, and `portfolio.py:529` prices a handful of
just-imported broker holdings once per reconcile. Moving them to the priority lane would save almost no calls and
would need six test fakes rewritten, so item 11 is closed without a code change.

## What was wrong
- Group 154 already added a quote pre-check and one WARNING per cycle, but `_fetch_history` still turned every
  failure (timeout, HTTP error, empty answer) into an empty list logged at DEBUG with an empty message. So "no
  history" could not be told apart: market-data timing out, or the symbol really having no history (new listing,
  unknown name).
- A symbol with no history was asked again every cycle.

## The fix
- `_fetch_history` leaves the last failure reason per symbol: exception class name (`ReadTimeout`), `HTTP 404`,
  `empty answer`. A good answer clears it. Return values are unchanged.
- The cycle WARNING now ends with the reasons, e.g. `Reasons: ReadTimeout x9, HTTP 404 x5, empty answer x2`.
- Definite cases (HTTP 404/400, empty answer, fewer than 6 daily candles) pause the symbol for
  `CANDIDATE_VOLUME_SHOCK_NOHIST_TTL_S` (default 6 h; 0 = off). Paused symbols are rejected with "No daily history on
  record ..." without any request, counted on one INFO line, and do not feed the "market-data failing" alarm.
  Timeouts, 429 and 5xx are never paused. A later good answer clears the pause.

## Things to check
- A new listing gains one candle a day, so a symbol with 1-5 candles is retried after 6 h, not every cycle.
- State is per process and resets on restart.
- After the next open, the WARNING shows whether the 16 symbols were timeouts (market-data) or 404/empty (no data).

## Tests
New `tests/test_group172_volume_shock_history_reasons.py` (19 cases incl. parametrized). `tests/conftest.py` gets an
autouse reset of the new state. Not run with real pytest/sqlalchemy (sandbox has neither): 16 of the cases (reasons
and pause) passed against stubbed imports; the 3 cycle-summary tests need the SQLite fixture and were not run.
