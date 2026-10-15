# Group 393 - opening replay (validation tooling)

`scripts/opening_replay.py` answers one question from your own ledger: what would the group 392 protection (half size, pause after 2 stop-outs, 09:15-09:45) have done to the opening trades you really took?

## Use
```
curl -s "http://127.0.0.1:8006/trades/history?limit=1000" > hist_$(date +%F).json   # once a day: retention keeps only ~3 days
python3 scripts/opening_replay.py hist_*.json [--size-mult 0.5] [--pause-stopouts 2] [--until 09:45] [--min-n 30] [--stop-mults 0.5,0.75] [--out DIR]
```
Files are merged by trade id. Output: a table of four scenarios (actual, size-only, pause-only, both) with trades, skipped, P&L, wins, stop-outs, worst trade, plus a verdict.

## Rules
- Replayed: settled trades (real exit price and P&L; not ERROR, OPEN or pending reconcile) opened 09:15 to `--until` IST on weekdays.
- Size cut: nearest share, 1-share trades unchanged, P&L scaled by new/old quantity.
- Pause: per IST day, a trade is skipped when `--pause-stopouts` STOP_HIT / STOP_BREACH_EXIT trades that scenario took had already closed before it opened.
- Verdict: `insufficient_sample` under `--min-n` (30) opening trades, else the sign of (both - actual) P&L.

## Limits
- P&L scaling ignores size-dependent slippage and charges; a skipped trade leaves no trace (its slot could have gone to a later candidate - not replayed).
- The ledger is a few days deep, so expect `insufficient_sample` for a while. No significance test.
- Real-trade: since group 398 the tool also reads real-trade per-trade records (`docs/GROUP398_REAL_TRADE_REPLAY_INPUT.md`).
- Stops: since group 395 the export carries each trade's own stop and the report has a Stops section (see `docs/GROUP395_STOP_BASIS_AND_EXPORT.md`). It still cannot say what stop the plan WOULD have set for a trade that used another path, and it cannot replay a wider stop.

## Tests
`scripts/test_opening_replay.py` (13 since group 395; 6 originally), in `service-tests.yml`.
