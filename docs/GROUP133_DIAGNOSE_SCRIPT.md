# group133 (2026-10-04) - one-command report for the two items that need the VM

Cumulative on group132. Tooling only: no application code changed, nothing to rebuild.

## Why
Two things cannot be settled from the sandbox: (A) whether the group 132 closed-market boot restore really skips the quote sweep, and (B) why HFCL / MTARTECH / STLTECH (and the rest of the "unresolved" names) do not resolve on AngelOne. Both need the VM's logs, DB row and a download of AngelOne's public scrip master.

## Files
- `scripts/diagnose_group132.sh` - the report. Read-only; prints no secrets.
- `scripts/diagnose_g132_row.py` - runs inside api-gateway: is the `stockky:surprise_scan:last_result` row present, when does it expire (expect ~7 days), is a stale read restorable.
- `scripts/diagnose_g132_scrip.py` - runs inside market-data-service: lists every scrip-master row for each name with `exch_seg`, `symbol`, `name`, `token`, and whether it resolves today (NSE + `-EQ`).

## Commands (repo root on the VM, e.g. ~/stockky-v2)
    bash scripts/diagnose_group132.sh 2>&1 | tee g132_report.txt
    NAMES="HFCL OTHER" bash scripts/diagnose_group132.sh 2>&1 | tee g132_report.txt   # extra names
    SINCE=6h bash scripts/diagnose_group132.sh                                      # log window (default 24h)

## Reading it
- A1 after the second closed-market restart should show `restored the last surprise/scan result, skipped the boot quote sweep`; `pre-warmed` instead means the sweep ran.
- A2 counts `GET /quote/` lines market-data-service served since api-gateway last started: a full sweep is ~1000, a few dozen is normal.
- A3 `row in DB: present, expires ~7 days` is the group 132 fix working; `ABSENT` right after the first boot on this build is expected (the old row was already purged).
- B2 `resolves today: NO -> only other series/segments exist` means the name trades under another series (for example `-BE`); that is the case where a loader change is worth considering. `no row at all` means a ticker/name problem instead.

## Checked here
Both probe scripts were run against a local sample scrip-master file (resolving, series-only, absent) and against a real SQL table in the absent and present states; `bash -n` passes on the shell script. The full pipeline against the real services is untested until you run it on the VM.
