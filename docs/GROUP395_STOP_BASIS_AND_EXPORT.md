# Group 395 - stop basis, trade-history export and the replay Stops section

## Why
Group 393's replay could not look at the group 391 opening-plan stops: the `/trades/history` export had no stop level, and nothing recorded whether a stop came from live bars, the 09:10 plan or the legacy path. This round supplies that evidence. It changes no entry, stop, size or exit decision.

## Change (position-stocks)
- `orders/adaptive.py`: `AdaptiveLevels.basis` = `"bars"` (live 5-minute bars), `"opening_plan"` (group 391 previous-day ATR plan) or `"legacy"` (tick-buffer ATR, % of the signal move, flat floor; also the default and the bar mode switched off). A plan lookup that errors counts as legacy. The group 340 target-cap rebuild keeps the tag.
- `models.py` / `db.py`: `scalp_positions.stop_basis VARCHAR(16)`, nullable, added by the existing idempotent `_ensure_columns` migration on boot (Oracle `VARCHAR2(16)`, Postgres `VARCHAR(16)`). Older rows stay NULL.
- `orders/entry.py`: both the automatic and the manual entry store `stop_basis` (`_stop_basis()` stores only a non-empty string tag).
- `main.py` `GET /trades/history` adds per trade: `stop_price`, `target_price`, `adaptive_stop_pct`, `adaptive_target_pct`, `breakeven_trigger_pct`, `stop_moved_to_breakeven`, `signal_price`, `stop_basis`. Additive; nothing existing was renamed.
  - `adaptive_stop_pct` / `adaptive_target_pct` are the ORIGINAL distances (never rewritten). `stop_price` is the value last stored: breakeven (off by default), trailing and the reconcile re-arm can move it. `stop_moved_to_breakeven` is also set by the trailing stop.

## Change (scripts/opening_replay.py)
New **Stops** section, per stop basis (and `all`), for opening trades whose export carries `adaptive_stop_pct`, `max_gain_pct`, `max_drawdown_pct` and `entry_price`:
- trades, stop-outs, median stop %, P&L;
- stop-outs that had already run at least 0.5R in favour ("stopped after running 0.5R" - an indicator of a stop that is too tight or an entry that is too early);
- non-stop-out trades whose drawdown came within 0.2R of their stop ("survivors" - a tighter stop would have killed them);
- a tighter-stop what-if (`--stop-mults 0.5,0.75`, fractions 0.1 to 0.99 of the trade's own stop, blank = skip): a trade whose recorded drawdown reached the tighter level is counted as stopped there at that loss, others keep their real P&L. Because the drawdown was recorded while the position was open, a trade that touched the level was certainly stopped by it, so this what-if is exact apart from tick sampling, slippage and charges.

## What it cannot do
- A WIDER stop cannot be replayed: the ledger has no price path after the real stop (excursion tracking ends when the position closes). Only the indicators above speak to "was the stop too tight".
- It cannot say what stop the plan would have set for a trade that used another path. Compare `opening_plan` trades with `legacy` trades instead (different stocks and times, so a first look, not a controlled test).
- Excursions come from the buffered tick stream sampled by the fast-reconcile loop, so a very brief spike can be missing.
- `stop_basis` is NULL on trades opened before this build (shown as `unknown`); a fresh export after deploy still gives them the stop and drawdown figures if the rows are still inside the retention window.
- No significance test; below `--min-n` (30) opening trades with stop data the section is labelled a first look.
- Real-trade-service is not covered: its stop comes from the daily ATR and its export differs.

## Use
```
curl -s "http://127.0.0.1:8006/trades/history?limit=1000" > hist_$(date +%F).json     # once a day (retention keeps ~3 days)
python3 scripts/opening_replay.py hist_*.json --stop-mults 0.5,0.75
```
After the deploy, take one export straight away (it already has the stop figures for rows still retained), then one a day.

## Deploy note
New nullable column: added on the next position-stocks boot by `_ensure_columns` (logged as "migrated - added missing column scalp_positions.stop_basis"). Nothing else to do. No new env variables.

## Tests
- `tests/test_group395_stop_basis.py` (21): tag for bars / plan / legacy / plan error / bar mode off / target cap; `_stop_basis`; stored by the automatic and manual entry; migration tuple and a real ALTER on a table that predates the column (idempotent); export fields, nulls for old rows, and the export feeding the replay tool end to end.
- `scripts/test_opening_replay.py` (13, 7 new): no-stop-data message, indicators by basis, tighter-stop maths and the exact boundary, unusable rows, markdown, `--stop-mults` parsing and CLI.
- Verification status: NOT run under pytest in this build (no pytest / sqlalchemy / network in the sandbox). The 13 replay tests ran as plain Python and pass; the six stop-tag paths in `adaptive.py` were run against the real module with import stubs and pass; the 21 position-stocks tests are unrun.
