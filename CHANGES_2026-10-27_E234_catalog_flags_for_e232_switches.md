# E234 - E232's three switches are on the Calibration page (2026-10-27)

Your VM run after E233: real-trade 5494 passed / 1 skipped; position-stocks 4436 passed, **1 failed**: `tests/test_e37_catalog_coverage.py`. E232 added `SCAN_HITS_ENABLED`, `DAY_MOVER_ENABLED` and `DAY_MOVER_SHORT_ENABLED` to `config.py` without a Calibration-page switch, which the E37 rule forbids. Nothing else failed, and E233's own tests passed on your VM.

## Changed (position-stocks `control/catalog.py`, three Flags, all shipped/recommended true = same behaviour as before)
- `SCAN_HITS_ENABLED` (Evidence recording, record only).
- `DAY_MOVER_ENABLED` (Entry guards, loosens: lists more candidates).
- `DAY_MOVER_SHORT_ENABLED` (Short selling, loosens; DAY_MOVER_ENABLED must also be on).
`check_control_catalog.py --probe` says LIVE-OK for all three (read at call time, so a switch saved on the page takes effect without a restart). `docs/ENV_REFERENCE.md` regenerated.

## Tests
Here: `test_e37_catalog_coverage.py`, `test_short_live.py`, `test_short_foundation.py`, `test_e232_scan_hits_day_mover.py` 34 passed; catalog/control tests 17 passed. Run `bash scripts/docker_test.sh position-stocks-service` on the VM to confirm 4437 passed.

## From your 27 Oct curl output (not a code fault)
- `/scan/hits` shows 0 scans / 0 symbols: the services had just restarted and the market was closed, so no scan had run. It fills on the next open session.
- `/scan/why/ZETWERK`: "not subscribed on the live feed (not in the stock list / no Dhan token)". Still open from E232: Zetwerk, Tonbo, ESDS and Mahindra Logistics are not in the stock list, so no scan can see them. Needs their exact NSE symbols and the market-data ticker map.
