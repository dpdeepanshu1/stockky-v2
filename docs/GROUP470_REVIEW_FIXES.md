# Group 470 (2026-10-20): fixes from the 20 Oct loss review (real-trade-service)

Evidence (196 REAL trades): gross -1351, charges 1783, net -3134. Biggest driver: 24 stop-hit exits (gross -1340, 17 filled beyond the stop,
3.1% on average, worst 17.6%) plus 5 gap-down emergency exits (-618). Second: overnight charges (85 one-day holds paid Rs16.8 each vs Rs2.0 same-day).
Entries in the 09:xx hour: 45 trades, 18% win rate, gross -866.

| Change | Setting | Default | Rollback |
|---|---|---|---|
| Overnight carry off | `OVERNIGHT_HOLD_ENABLED` (config.py) | **false** (was true) | `OVERNIGHT_HOLD_ENABLED=true` |
| One entry cut-off every day | `ENTRY_LAST_TIME_IST` | **14:00** (blank = off) | set blank |
| Size on real stop-out cost | `STOP_OVERSHOOT_ALLOWANCE_PCT` | **1.0** (0 = old sizing) | `=0` |
| Opening-range-breakout recorder (record only) | `ORB_MODE` | **shadow** (`off` stops it) | `ORB_MODE=off` |

* Cut-off: `calendar_gate.late_entry_block_reason()`, wired into Gate 4d (gate_tag `late_day`). Independent of `CALENDAR_RULES_ENABLED`.
  Square-off stays at `EOD_SQUAREOFF_TIME_IST` 15:00, so the bot exits before Dhan's own auto square-off (Rs20 + GST per order).
* Sizing: qty = risk cap // (entry - stop + entry x allowance%). Never below 1 share when the plain cap allows 1, so no new WAITs.
  At Rs18k equity, 5% risk, entry 100 / stop 95: 180 -> 150 shares.
* ORB recorder (`entry_engine/orb_shadow.py`): writes `flow_json["orb"]` on every entry decision 09:15-10:30: 5-minute range (09:15-09:20),
  would-enter verdict (price above range high and VWAP, spread, range width), limit price, range-low stop. Nothing is ordered or blocked.
  Needs 30+ would-enter rows with a result before any live use. There is no live mode.
* Not changed: `DEFAULT_RISK_PER_TRADE_PCT` (5.0) and the Rs3,000 position cap. Review says do not raise size until 30+ trades show positive
  expectancy after charges. Consider lowering risk to 1-2% separately.
* Not built (needs design / data): gap-down guard, stop-overshoot guard in the exit loop, skipped-candidate flow stamping.

## Check on the VM
* `BROKER_STOP_ENABLED`: the code default is true (group 377) but the 12 Oct live config showed False. Confirm the `.env` and the
  startup log (no "SAFETY: BROKER_STOP_ENABLED is off" warning).
* `OVERNIGHT_HOLD_ENABLED` / `ENTRY_LAST_TIME_IST` / `STOP_OVERSHOOT_ALLOWANCE_PCT` in `.env` override these defaults.
* Rebuild + redeploy real-trade-service.

## Tests
`tests/test_group470_review_fixes.py` (7), plus `test_blank_flag_env.py` (overnight default). `tests/conftest.py` gains three autouse
fixtures (cut-off off, overnight on, overshoot 0) so older tests keep their meaning.
