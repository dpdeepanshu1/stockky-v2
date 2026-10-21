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
* Without `ADMIN_TOKEN` the REAL sections are skipped and the report says so.
* No code path in the trading services changed in this group.

## What to look for
* `charges_split`: same-day trades near Rs 2 each means the charges problem was carried delivery positions; if same-day is still far above that, send the Dhan contract notes.
* `/review-fixes/status` `size_ladder`: level 0 is expected until 10 positive days exist.
* `gate_scorecard`: a gate with 30+ samples, mean forward return clearly above cost and a high target-first rate is blocking winners; otherwise keep it.

## Tests
`scripts/test_vm_review_evidence.py` (5, fake curl/docker: every section, no secrets, no-token skip, failed export, connection failure, section cap). Added to the CI scripts step and path filters.
