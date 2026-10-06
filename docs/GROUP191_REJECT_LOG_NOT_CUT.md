# Group 191 - "CANDIDATE REJECTED" log lines no longer cut at 150 characters

Cumulative on group 190. Item 17 of the 2026-10-06 boot-log list. Rebuild real-trade-service.

## What the log showed
The REDINGTON rejection ended at "Wait for a breakout or" and the HFCL one stopped before the closing brace of the
returns dict, so both looked cut off by the logger.

## Cause
Not the logger. `candidate_engine/candidates.py` printed `reject[:150]` on both the standard track
(`CANDIDATE REJECTED`) and the volume-shock track (`VOLUME_SHOCK CANDIDATE REJECTED`). The reasons are longer than that:
the "weak momentum" and "incomplete history" reasons embed the whole timeframe-returns dict, and the near-resistance
reason ends with a sentence of advice. The stored reason (what the UI and the DB see) was never cut.

## Change
New `_reject_log_text()`: the limit is `CANDIDATE_REJECT_LOG_MAX` (default 500, 0 = never cut, bad value = 500) and a
line that is cut still ends in " ..." so a real truncation can be told from a missing newline. Both log sites use it.

## Not changed
The reject reasons themselves, and the log volume per rejection beyond the longer text (one line per rejected symbol,
as before). 500 characters covers the longest reason built in this file.

## Tests
`tests/test_group191_reject_log_not_cut.py` (6 cases, using HFCL- and REDINGTON-shaped reasons). Full real-trade-service
suite in the sandbox: 3335 passed, 4 failed + 1 error (`test_group171_held_quote_calls` x4,
`test_group172_volume_shock_history_reasons` x1) - identical on the uploaded zip.
