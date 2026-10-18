# Group 433: calibration survives a restarting gateway, records can include broker-imported positions

Tooling and a read-only endpoint option. No trading decision changes.

## 1. Calibration fetch (`scripts/fetch_history_for_backtest.py`, `scripts/vm_calibrate.sh`)
Two 18 Oct VM runs failed at the first step for every seed ("Connection refused", then "Connection reset by peer") because the gateway was
restarting or busy, which left `hist_7` with 2-3 symbols and made the backtests that followed meaningless. Now:
- reading `--universe-url` is retried (`--universe-retries`, default 5, `--universe-wait`, default 5 s growing each time);
- `vm_calibrate.sh` first waits for `$API_GATEWAY_URL/health` (`GATEWAY_WAIT_S`, default 180 s; skipped with `SYMBOLS=` or `DRY_RUN=1`) and then goes on even if it never answers (a warning is printed).

## 2. Records can include broker-imported positions (`real-trade-service/trade_records.py`, `main.py`, `scripts/export_records.py`, `scripts/records_report.py`)
`GET /positions/{mode}/records` leaves out `broker_imported` positions (the system did not enter them). On 18 Oct 61 of the 62 REAL positions
opened since 1 Oct were flagged that way, so the export showed none of October. New, all opt-in or additive:
- `?include_imported=true` on the endpoint (default false, so every existing caller is unchanged);
- every record carries `origin` = `system` | `imported`;
- `python3 scripts/export_records.py --include-imported`;
- `records_report.py` adds an "origin" table, so the two kinds are never mixed in one total unnoticed.
An imported row is a position the portfolio sync found at the broker; its `opened_at` is the import time, so entry hour and hold time for those rows are the import moment, not the real purchase.

## Tests
`TestGroup433IncludeImported` (real-trade), export flag and universe-retry tests (scripts), gateway wait test in `test_vm_calibrate.py`
(the other two calibrate tests now set `GATEWAY_WAIT_S=0`).
