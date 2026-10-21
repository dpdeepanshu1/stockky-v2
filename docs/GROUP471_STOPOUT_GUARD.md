# Group 471 (2026-10-20): whole-day stop-out guard (real-trade-service)

Why: stop-hit exits were the biggest loss driver in the 20 Oct review (24 trades, gross -1340, 17 filled beyond the stop; 5 gap-down exits -618).
Group 394 pauses entries after 2 stop-outs only until 09:45. This extends the idea to the whole session.

| Rule | Setting | Default |
|---|---|---|
| Master switch | `STOPOUT_GUARD_ENABLED` | true |
| Modes | `STOPOUT_GUARD_MODES` | REAL,DEMO |
| No new entry for the rest of the day after N losing stop-outs | `STOPOUT_DAY_PAUSE_N` | 3 (0 = off) |
| A symbol stopped out today is not re-bought today | `STOPOUT_SYMBOL_BLOCK` | true |

* Counted from trade_position_events + trade_positions (a restart does not reset it). Counts `stop_hit`, `broker_stop`, `emergency_gap_down` closes with negative P&L.
* Day pause: evaluate_mode returns before the loop; candidates stay queued (not consumed). Symbol block: WAIT with gate_tag `stopout_symbol` (Gate 4d).
* Automatic path only; manual orders are never touched. Fails open on any error.
* TIGHTENS live entry rules. Rollback: `STOPOUT_GUARD_ENABLED=false`.
* Tests: `tests/test_group471_stopout_guard.py` (5, pure parts). The evaluate_mode wiring was NOT run here (no sqlalchemy); CI is the first full run.
