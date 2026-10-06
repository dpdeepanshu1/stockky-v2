# Group 190 - silence the dhanhq pandas DtypeWarning

Cumulative on group 189. Item 18 of the 2026-10-06 boot-log list. Rebuild real-trade-service and
position-stocks-service.

## What the log showed
`DtypeWarning: Columns (...) have mixed types. Specify dtype option on import or set low_memory=False.` on every
security-master load. It comes from `client.fetch_security_list(mode="compact")` inside the dhanhq SDK, which reads
Dhan's large scrip CSV with pandas. Harmless here: `_load_security_cache` only reads a few string columns per row.

## Change
`execution/dhan_client.py` in both services: new `_fetch_security_list_quiet(client)` wraps that one SDK call in
`warnings.catch_warnings()` and ignores only a warning whose text matches `Columns (...) have mixed types` (old and
new pandas wording). Every other warning still shows, nothing outside the call is filtered, the filter list is restored
afterwards, and SDK exceptions propagate unchanged into the existing CSV-download fallback.

## Not changed
The SDK still parses the CSV the same way (no `dtype=` / `low_memory=False` - that is inside dhanhq). Any other pandas
`DtypeWarning` from elsewhere is deliberately left visible.

## Tests
`tests/test_group190_security_list_quiet.py` in both services (5 cases each): both wordings silenced, other warnings kept,
no filter leak, exception propagates, `mode="compact"` still passed. Sandbox: dhan_client test files pass (real-trade 135,
position-stocks 115). Full real-trade-service suite: 3329 passed, 4 failed + 1 error (`test_group171_held_quote_calls`
x4, `test_group172_volume_shock_history_reasons` x1) - identical on the uploaded zip.
