# E132 - swing momentum lane, S3 to S5 (2026-10-25)

Finishes what the S2 zip left open and puts the plan's rules into live code.
Ships in **shadow**: nothing is bought until the owner sets `SWING_MODE=live`. REAL stays blocked by `SWING_LIVE_REAL_ENABLED=false`.

## Plan vs code
| Plan item | Where it lives | Default |
|---|---|---|
| Signal: top 20% vol-adjusted momentum AND above 200-day average | `swing/momentum.py` (`screen`), `lane.pick_candidates` | on |
| SWING_MODE off / shadow / live | `config.SWING_MODE`, catalog | shadow |
| Evening scan, shadow ledger, nightly settle | `swing/engine.py`, `swing/repo.py`, tables `trade_swing_ledger`, `trade_swing_runs` | on (shadow) |
| CNC entry at ~09:25, own size cap and limits | `swing/live.py::entries` via `manual_engine.evaluate_manual_order(swing=...)`; `SWING_MAX_TRADE_VALUE` 20000, `SWING_MAX_POSITIONS` 2, `SWING_MAX_EXPOSURE_PCT` 40, `SWING_MAX_PER_SECTOR` 1 | live only |
| Entry next open + 0.10% slippage; skip gap-up > 5% | `lane.entry_levels`, `lane.gap_too_big` | on |
| Stop ATR14 x 1.5, clamp 2-6%, ratchet to close-stop%, never lowered, gap exits at open | `lane.stop_pct_from_atr`, `lane.advance`, `lane.ratcheted_stop` | on |
| EOD square-off bypass for swing positions | `auto_pilot._eod_squareoff` + `swing.live.eod_holds` (sells everything if not live or on any error) | on |
| Exit engine: no target/partial/breakeven/trail/time-stop for swing | `exit_engine/exit.py` block 1b | on |
| 15:00 re-check, hard exit on day 5 | `lane.hold_verdict` (fail-closed) | on |
| Vetoes: earnings window, negative news, stressed market | `engine.entry_vetoes`, `SWING_EARNINGS_WINDOW_DAYS`, `SWING_BLOCK_STRESSED` | on |
| Auto-pause: 3 losses in a row, gap-through-stop | `lane.pause_state`, `/swing/resume` | on |
| Broker stop, swing only | `broker_stop.swing_only()`, `SWING_BROKER_STOP_ENABLED` | off |
| Shadow verdict (USEFUL / NO_EDGE / NOT_ENOUGH_DATA) | `swing/stats.py`, `scripts/swing_shadow_report.py`, weekly runner job | on |
| S3a snapshot / universe scripts | `scripts/swing_snapshot.py`, `scripts/swing_universe.py` | - |
| S4 real-order probe | `swing/probe.py`, `scripts/dhan_swing_probe.py`, job `dhan_swing_probe` (ops, in no pack, places a real order) | manual |
| Protected switches | catalog group "Swing lane"; all keys in `autocalib.PROTECTED` | - |
| Admin routes | `/swing/status /ledger /snapshots /report /scan /resume /probe` | - |

## Verified (run here)
- New `tests/test_swing_lane.py`: 27 tests pass. They include exact parity of `lane.advance` with the replay's `walk_trail` on 300 random paths, plus stop and ATR parity with the replay.
- Existing: swing backtest and momentum tests, runner tests (46 pass), compose and autocalib tests.
- Guards: control catalog, shared copies, table-name collisions, secrets, merge conflicts, requirements drift.

## Not run - be honest
This sandbox has no SQLAlchemy, FastAPI or httpx. So `SqlRepo`, `swing/live.py`, `swing/probe.py`, the new routes, the auto_pilot and exit-engine edits were only compiled and checked statically. Run the full service test suite on the VM before relying on them. Nothing was tested against Dhan.

## VM steps
1. Deploy; tables are created by `create_all`. Leave `SWING_MODE=shadow`.
2. After 16:00 IST confirm `/swing/status` shows a scan; let it run for weeks.
3. Read `/swing/report` or the weekly job. Only on USEFUL consider DEMO `live`.
4. Run `dhan_swing_probe` once (S4) before any REAL.
5. REAL needs both `SWING_MODE=live` and `SWING_LIVE_REAL_ENABLED=true`, set by you, and not while the service is on TURN OFF advice.

## Plan correction
Plan costs: DP charge is Rs 14.75 per sell, so a Rs 3,000 trade costs about 0.71% round trip. Shadow costs use `cost_model` (delivery).
