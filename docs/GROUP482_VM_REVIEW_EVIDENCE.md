# Group 482 - one-command review evidence (2026-10-21)

The open review items (where the ~Rs 10.6 per trade of charges goes, whether the size ladder moved, whether the position-stocks gates block winners) all need
numbers from the VM. `scripts/vm_review_evidence.sh` collects them in one read-only run.

    ADMIN_TOKEN=<real-trade /auth/login session token> bash scripts/vm_review_evidence.sh
    DAYS=30 SINCE=2026-10-20 ADMIN_TOKEN=... bash scripts/vm_review_evidence.sh      # export window / charges-split start day
    CAP=6000 ...                                                                      # longer sections

It writes and prints `review_evidence.txt` (paste it back). Sections: date, compose services, review `.env` lines (no secrets), position-stocks and
real-trade `/review-fixes/status` (size ladder level, stop-out pause, ORB counters), `/charges/REAL/cumulative`, a fresh REAL trade export,
`charges_split.py` and `records_report.py` on it, and `gate_scorecard.py` inside the position-stocks container.

* Read-only: GET requests, `docker compose exec` of a read-only script, local python on the export. No order, setting or file in the services changes.
* Every call prints its HTTP status and time, so an empty section can be told apart from a 401, a 404 or a timeout.
* Without a token the REAL sections (and the position-stocks status, which needs it) are skipped and the report says so.
* **Login built in (482b):** `ADMIN_USERNAME=<your real admin user name> bash scripts/vm_review_evidence.sh` asks for the password silently and logs in itself (`ADMIN_PASSWORD` for a non-interactive run). The password goes to curl on stdin, never on a command line; neither it nor the token is written to the report. `<...>` in a docs example is a placeholder, not text to type.
* The gate table is no longer cut off (`SCORECARD_CAP`, default 9000). `gate_scorecard.py` now says whether skipped rows have no bar folder for that day at all (recorder not running yet) or the symbol was simply not recorded.
* No code path in the trading services changed in this group.

## What to look for
* `charges_split`: same-day trades near Rs 2 each means the charges problem was carried delivery positions; if same-day is still far above that, send the Dhan contract notes.
* `/review-fixes/status` `size_ladder`: level 0 is expected until 10 positive days exist.
* `gate_scorecard`: a gate with 30+ samples, mean forward return clearly above cost and a high target-first rate is blocking winners; otherwise keep it.

## Tests
`scripts/test_vm_review_evidence.py` (7, fake curl/docker: every section, no secrets, no-token skip, failed export, connection failure, section cap, login ok / failed). Added to the CI scripts step and path filters.

## Finding from the 19 Oct 2026 Dhan contract note (no code change needed)
13 same-day round trips, brokerage 16.40, NSE 1.68, SEBI 0.05, GST 3.26, stamp 1.00, STT 7.00 = **Rs 29.39, about Rs 2.26 per round trip**, no DP charge and no auto-square-off fee.
`charges_ledger.order_charges` on the same 26 fills gives Rs 29.03 (brokerage, exchange, SEBI and GST match to the paisa; STT and stamp differ because Dhan rounds them per order). Gross P&L was -Rs 9.20 on Rs 27,316 bought, so charges were most of that day's loss.
So the intraday rate card is modelled correctly, and the ~Rs 10.6 per trade average came from other trades (delivery / carried), not from a missing fee. The auto-square-off fee is not added: there is no evidence for it.
