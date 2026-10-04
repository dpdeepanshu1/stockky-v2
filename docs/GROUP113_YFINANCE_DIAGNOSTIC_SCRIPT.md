# group113 (2026-10-04) - item 6: one-command yfinance diagnostic for the VM

Cumulative on group112. No application code changed; nothing to rebuild.

## Why
Item 6 (yfinance returns nothing on the VM) is an environment question: Yahoo blocking or rate limiting the VM's IP, a user-agent issue, a library problem, or DNS. It cannot be decided from the repo. The script gathers the evidence in one run.

## Run
From the repo root on the VM (`~/stockky-v2`), ideally while the market is open:

    bash scripts/diagnose_yfinance.sh 2>&1 | tee yf_report.txt

Then paste `yf_report.txt`. Optional: `SINCE=6h bash scripts/diagnose_yfinance.sh` widens the log window (default 3h).

## What it does (read-only)
1. DNS lookup of the four Yahoo hosts.
2. `curl` of the chart endpoint for RELIANCE.NS on query1 and query2, with the default curl user-agent and with a browser user-agent: HTTP status, time, and the first 140 characters of any non-200 body.
3. Inside the `api-gateway` container: yfinance version, then `history(period="1d", interval="1m")` for RELIANCE.NS, TCS.NS and ^NSEI with row counts, time and any exception.
4. The newest 40 yfinance/Yahoo/429/"Movers:" log lines from `api-gateway` and `market-data-service`.
Nothing is changed; no secrets or tokens are printed (the log lines are filtered to yfinance/Yahoo/rate-limit terms and cut to 260 characters, but skim the report before pasting).

## How to read it (what each result would mean)
- curl 429 or a "Too Many Requests" body on every row: Yahoo is rate limiting the VM's IP.
- curl 200 with a browser UA but not the default UA: a user-agent block; yfinance would need its session settings looked at.
- curl 200 on both but yfinance rows=0 or an exception: a library or version problem rather than the network.
- curl timeouts or DNS failures: network or firewall on the VM.
- Everything 200 with rows > 0 while the market is open: the failure is intermittent; the Movers fallback (group109) already covers the dashboard.

## Tested here
`bash -n` passes and the script ran in the sandbox: DNS and the curl section execute, and sections 3 and 4 print the "docker not found" message and skip. The sandbox's network allow-list answered 403 for Yahoo, so no Yahoo result was obtained here. The yfinance probe and log sections have not been run against a real container.
