# E232 - why a mover was not picked: scan-hit record, day-mover path, /scan/why (2026-10-27)

## Finding (from the 2026-10-27 review of 18 top movers)
- Only 6 of 18 appeared in `/candidates/log`; 12 never did (Zetwerk +12.5%, Mahindra Logistics +11.3%, Data Patterns +9.2%, Tonbo, Apollo Micro, Netweb ...).
- The log is not "every stock the scan found": `main.py` takes only the top `max(QUALITY_GATE_TOP_N, SHORTLIST_MAX)` = 5 ranked names per cycle to the quality gate and the log. A name ranked 6th every cycle left no trace.
- The scan only sees ROLLING moves: +1.0% in 5 min or +2.5% in 60 min (1m / 15m are off via DISABLED_SCAN_WINDOWS). A stock that climbs steadily all day, or gaps at the open, never trips either.
- Not fixed here (needs the exact symbols / a market-data ticker map): Tonbo, ESDS, Mahindra Logistics are not in the stock list; Zetwerk's 1-minute history fetch answers HTTP 404 (already non-fatal: counted as a backfill error and retried).
- Short side: a faller must also be ranked bearish by the board (`short_entry._bear_rows`) before it can pass; that second filter is unchanged.

## Changes (position-stocks)
1. `screening/scan_hits.py` (new): every symbol the long and short scans listed, per IST day: first/last seen, scans, best/last rank, best move + window, quality-checked cycles. Memory + `SCAN_HITS_DIR/scan_hits_<day>.json` (atomic, every SCAN_HITS_DUMP_S, reloaded after a restart, pruned after SCAN_HITS_KEEP_DAYS). RECORD ONLY; unwritable directory turns the file write off, memory keeps working. Hooked in `main.py` (after `scan`, after the top-N slice) and `orders/short_entry.py`.
2. `screening/engine.py`: day-mover path. A stock no rolling window listed, whose move since the previous close is between DAY_MOVER_MIN_PCT (4) and DAY_MOVER_MAX_PCT (20), is listed once as window `day` (window_minutes 1440, label "day"). Same volume floor, spread cap, quality gate, range gate, stop-size floor, sizing and shortlist as any candidate; previous close unknown = not listed (never guessed). Score = min(move, 2 x MIN) x DAY_MOVER_SCORE_WEIGHT x liquidity x range x VWAP multipliers. Mirrored in `scan_shorts` (DAY_MOVER_SHORT_ENABLED). DAY_MOVER_ENABLED=0 restores the old scan exactly.
3. `screening/scan_why.py` (new) + routes `GET /scan/why/{symbol}` and `GET /scan/hits?never_checked=true&side=&limit=`: the scan's own checks in order against the live feed (subscribed, ticks, volume floor, spread, price floor, each window, day move) plus today's hits, with a one-line verdict. Read only.

## Env (all in config.py, ships on)
SCAN_HITS_ENABLED, SCAN_HITS_DIR (/data/bars/_scan_hits, inside the existing bar-records volume), SCAN_HITS_DUMP_S, SCAN_HITS_KEEP_DAYS, DAY_MOVER_ENABLED, DAY_MOVER_SHORT_ENABLED, DAY_MOVER_MIN_PCT, DAY_MOVER_MAX_PCT, DAY_MOVER_SCORE_WEIGHT. docs/ENV_REFERENCE.md regenerated.

## Tests
`tests/test_e232_scan_hits_day_mover.py`: 11 new (day mover long/short/off/min/cap/volume/unknown prev close/no duplicate with a rolling hit; hit record never-checked, restart reload, unwritable dir, off; why-not-picked). Ran under a stand-in runner with stubbed websockets/pyotp (pytest not installed in the sandbox); `test_short_step5_scan` (8) and `test_screening_engine` (1) also pass there. NOT yet run on the VM: the real pytest suites.

## VM
    curl -s "http://localhost:8006/scan/why/ZETWERK" | python3 -m json.tool
    curl -s "http://localhost:8006/scan/hits?never_checked=true&limit=50" | python3 -m json.tool
(from the host, `docker exec` the container if 8006 is not published). After a rebuild, the day-mover names show as window `day` in `/candidates/log`.
