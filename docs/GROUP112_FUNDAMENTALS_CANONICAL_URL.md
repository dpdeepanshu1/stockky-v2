# group112 (2026-10-04) - item 4 leftover: bare-ticker fundamentals URLs in analyze() and the decision service

Cumulative on group111. Rebuild: `docker compose build analysis-intelligence-service decision-prediction-service && docker compose up -d`.

## Cause
group88 made the peer step request `/fundamentals/INFY.NS`. Two other callers still requested `/fundamentals/INFY`: `fundamental/main.py::analyze()` and `decision-prediction-service/prediction/main.py::_fetch_fundamentals`. market-data normalises both to the same cache key (`fundamentals:INFY.NS`), so this was never an extra Yahoo call, only two URL spellings for one company in the market-data log.

## Change
New `_md_fundamentals_symbol()` (identical copy in each service; the services share no code):
- a plain ticker (`[A-Z0-9&-]+` after trim and upper-casing) becomes `<TICKER>.NS`;
- everything else is returned exactly as given: names with spaces (`KFIN TECHNOLOGIES`, `NIFTY BANK`), `^` symbols, symbols that already end in `.NS`/`.BO`, blank values, and the index names market-data maps itself (anything starting `NIFTY`, plus `BANKNIFTY`, `SENSEX`, `INDIAVIX`, the same rule as market-data's `normalize_symbol`).
Used for the request URL in both places, and for the `path` reported to the rate-limit reporter in `analyze()` so it names the URL actually requested.

market-data strips `.NS`/`.BO` before its own alias and index mapping, so a suffixed ticker takes the same path as the bare one.

## Not changed
- The `symbol` returned in the `analyze()` result is still upper-cased bare (`INFY`), and the IndianAPI fallback still receives the original symbol.
- No change to market-data-service.

## Behaviour to know
- The rate-limit event `path` for `analyze()` is now `/fundamentals/TCS.NS` instead of `/fundamentals/TCS`. If anything groups those events by exact path, old and new rows will not match.
- Value is cosmetic and consistency only; if you would rather not carry two copies of the helper, reverting this group is safe.

## Tests
- `analysis-intelligence-service/tests/test_fundamental_main.py`: 18 URL cases (plain, lowercase, padded, `M&M`, `BAJAJ-AUTO`, `360ONE`, suffixed, indices, spaced names), both spellings make one request, blank passthrough (3); the rate-limit path assertion updated to `TCS.NS`.
- New `decision-prediction-service/prediction/tests/test_fetch_fundamentals_symbol.py`: helper cases (15 + 3 blank), fetch URL for each case with the payload still normalised, failed fetch still returns `{}`.

## Run here
No pytest/fastapi/httpx in the sandbox, so the suites were NOT run. Both modified files and both test files compile, and the helper cases were checked in isolation (both copies byte-identical). On the VM: `bash run_tests.sh` in analysis-intelligence-service, and `python3 -m pytest prediction/tests/test_fetch_fundamentals_symbol.py` from `services/decision-prediction-service/prediction`.
