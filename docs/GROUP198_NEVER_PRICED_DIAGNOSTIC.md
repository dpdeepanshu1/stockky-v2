# Group 198 - diagnostic for "symbols that never price" (no service code changed)

Cumulative on group 197. Item 3 of the open list (STEAMHOUSE, SGRL, KENNAMET, and the ELEVATE / ROSSTECH timeouts). Nothing to rebuild.

## What I checked in the code
- real-trade-service already pauses a symbol after repeated "no price" answers (group 160, 30 min doubling to 6 h, and the pause survives a
  restart: group 183b). Timeouts are deliberately not counted by that pause.
- market-data-service answers a symbol that failed the whole waterfall twice from a "no price" cache for 5 min doubling to 1 h
  (`source: "negative_cache"`, group 161), and has a `/last-close/{symbol}` route.
- The bhavcopy tier keeps only series EQ / BE / BZ (`bhavcopy.process_bhavcopy_rows`) and AngelOne's feed resolves EQ, then -BE, then -BZ
  (groups 135/136). A stock on the SME board (series SM / ST) is therefore "in no bhavcopy" and has no AngelOne EQ row by design, which would
  explain `/last-close` 404 for these names. **That is a hypothesis: nothing in the repo or in the zip records these three names' series.**
- So the pausing machinery is in place, and the open question is which of three different situations each name is in (SME board, a
  ticker that is simply wrong, or a real EQ stock whose sources are failing). The fix is different for each, and changing the universe filter
  without knowing would drop tradable names or keep untradable ones. I did not change service code.

## What this group adds
`scripts/diagnose_never_priced.sh` (+ `scripts/diagnose_never_priced.py`), same style as `diagnose_group132.sh`; read-only, no secrets:
1. AngelOne scrip-master rows per name with the series suffix (-EQ / -BE / -BZ / -SM), reusing `diagnose_g132_scrip.py`.
2. Timed `GET /quote` (twice) and `GET /last-close` from inside market-data-service, with a one-line reading: priced, 404 delisted, full waterfall
   with no price (and how many seconds it held a worker), negative-cache answer, or timeout.
3. Log lines for those names from market-data, real-trade and api-gateway, and whether each is in the current `/scan/universe` and momentum movers
   (this shows which source feeds them in).

Run on the VM from the repo root:
`bash scripts/diagnose_never_priced.sh 2>&1 | tee never_priced_report.txt`   (other names: `NAMES="FOO BAR" bash ...`, window: `SINCE=6h`)

## Tested here
`bash -n` and `py_compile` pass; the probe was run against a local fake market-data server for the five answer shapes (priced, negative cache,
404, slow/timeout, connection refused). The scrip-master and docker parts only run on the VM.

## What the report decides
- Only `-SM` rows, no `-EQ/-BE/-BZ`: SME stocks. Then the right change is to keep that series out of the universe at the source (an explicit decision
  for you: Dhan intraday is normally not available on them).
- No row at all and 404 everywhere: a wrong / renamed ticker; the news or movers source that produced it needs the same name hygiene as groups 115-117.
- EQ row exists but `/quote` is slow with no price: a market-data source problem for that symbol; the report shows which.
