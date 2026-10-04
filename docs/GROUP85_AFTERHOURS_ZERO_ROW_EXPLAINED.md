# group85 (2026-10-04) - item 17: after-hours scan says why it wrote 0 rows

Cumulative on group84. Run `bash run_tests.sh` in real-trade-service on the VM.

## What changed
`watchlist_engine/afterhours_scan.py::run_afterhours_scan` now logs, at INFO:
- when nothing scored: `no scored items found this pass - 0 rows written (<N> RSS item(s) fetched: <a> older than the max news age, <b> matched no NSE symbol, <c> scored <= 0; <k> bulk/block-deal hit(s))`, or "every RSS feed returned 0 items" if all feeds were empty;
- when items scored but nothing was written: `0 rows written - all N scored symbol(s) already stored ... (nothing new to write)`, or `... upsert(s) FAILED (see the warnings above)`;
- in both cases a `funnel` line per feed: `Moneycontrol: 12 items, 3 stale, 7 no-symbol, 1 score<=0, 1 scored | ... | bulk/block: 0 hit(s)`.

The Telegram summary says "N failed to save" when some upserts failed. No env vars. No scoring/DB behaviour change.

## Not changed
Items 22 and 26. Note: real-trade-service `_RSS_FEEDS` still lists Moneycontrol, which returned HTTP 403 from the VM in the news service; the new funnel line will show it as `0 items` if it does the same here.
