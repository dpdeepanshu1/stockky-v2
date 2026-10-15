# Group 398 - replay a real-trade export

`scripts/opening_replay.py` (groups 393 / 395) could only read the position-stocks `/trades/history` export. This round lets it read real-trade-service's per-trade records too. Tooling and evidence only: no service decision changes.

## Change
- `trade_records.py` (real-trade): each record gains `opened_at`, `closed_at` (UTC ISO) and `quantity` (shares bought to open the position: filled quantity of its BUY orders, else their order quantity, else None). These come with `GET /positions/{mode}/records`; the report route is unchanged.
- `scripts/opening_replay.py`: reads the records file (an object with a `"records"` list) as well as the ledger export (`"trades"` list). A real-trade record is converted to the ledger shape:
  - P&L = `gross_pnl`; size = `quantity`; times = `opened_at` / `closed_at`; stop figures = `stop_pct`, `max_gain_pct`, `max_drawdown_pct`, `stop_basis` (groups 396 / 397).
  - **Stop-out = a stop exit that lost money** (`exit_reason` stop_hit / broker_stop / emergency_gap_down AND negative P&L). That is exactly what group 394's pause counts, so the replay mirrors real-trade. A stop exit that did not lose is labelled `STOP_EXIT_NOT_LOSING` and is not a stop-out.
  - A record with no known exit price gets `entry + P&L / quantity`. A record with no quantity or no opening / closing time is not replayed.
  - Rows merge by (mode, position id) across files, so a daily export builds history beyond the `days` window.
- Position-stocks and real-trade files must be replayed in separate runs; mixing them exits with code 2 and a message.

## Use
```
curl -s "http://127.0.0.1:8007/positions/REAL/records?days=14&limit=1000" > rt_$(date +%F).json     # adjust host / port and admin auth to your setup
python3 scripts/opening_replay.py rt_*.json --stop-mults 0.5,0.75
```
Use `/positions/DEMO/records` for DEMO. Only trades opened after groups 396 / 397 have stop and excursion figures; older ones are still replayed for size and pause but are left out of the Stops section.

## Limits
- P&L is gross (before charges), scaled by quantity, like the ledger replay; slippage and size-dependent charges are ignored, and a skipped trade is assumed to leave no trace.
- The records route keeps whatever `days` you ask for (1-60) and `limit` (1-1000); a real-trade DB with no retention cleanup means more history than position-stocks' ~3 days, so verdicts reach 30 opening trades sooner.
- Real-trade's size cut works on the risk-sized quantity; replay scales the final quantity, which is the same thing except for rounding.
- Excursion is sampled once per exit cycle (group 397), so the tighter-stop counts are bounds.
- No significance test; below `--min-n` (30) the verdict is `insufficient_sample`.

## Tests
- `scripts/test_opening_replay.py` (21, 8 new): losing stop is a stop-out and a profitable stop exit is not (this caught a labelling bug while writing it), exit-price derivation, records replay like ledger rows, rows without size or time skipped, stop fields feed the Stops section, merge by position id and mode, mixed input refused, CLI.
- `services/real-trade-service/tests/test_group398_record_replay_fields.py` (6, pure): quantity from fills, from order quantity, None, two buys, UTC times, helper edge cases.
- Verification status: both ran as plain Python and pass. Not run under pytest (none in the sandbox), but neither file needs sqlalchemy or fastapi. The records route itself (`main.py`) is unchanged.
