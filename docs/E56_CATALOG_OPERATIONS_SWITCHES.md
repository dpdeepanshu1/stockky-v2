# E56 - calibration page: Operations switches

Added to `services/*/control/catalog.py`, group **Operations**:

| Service | Switch | Default | Applies |
| --- | --- | --- | --- |
| real-trade | `DAILY_REPORT_ENABLED` | true | live |
| real-trade | `EDIS_MORNING_CHECK_ENABLED` | true | live |
| position-stocks | `SHORTLIST_ENABLED` | true | live |
| position-stocks | `ADAPTIVE_HISTORY_ENABLED` | true | live |
| position-stocks | `DHAN_ORDER_WS_ENABLED` | false | restart (listener starts at boot) |

`scripts/check_control_catalog.py --probe` showed every live one is read at call time (no import-time copies), and the script passes for both catalogs.

Left off the page on purpose: `SHARED_GATES_FAIL_CLOSED`, `DHAN_TOTP_ENABLED`, `ENTRY_PRODUCT_MODE` (safety / real-money settings that should need a deliberate .env edit).

Checked and already present (an older audit listed them as missing): the 10 Real Trade and 10 Position Stocks gate switches it named, `ACCOUNT_MAX_DAILY_LOSS_PCT`, `RISK_MAX_ENTRIES_PER_DAY`, `POSITION_STOCKS_MAX_ENTRIES_PER_DAY` (E39), `ENTRY_LAST_TIME_IST` (Real Trade general cutoff).

Not run: pytest (not installed in the build sandbox). Run `bash run_tests.sh` in each service on the VM.
