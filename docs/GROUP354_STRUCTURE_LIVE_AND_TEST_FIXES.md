# Group 354 - structure gates LIVE, position-stocks daily-structure gate, 11 test fixes

## Live by default (each can be loosened with its env var)
- real-trade: `STRUCTURE_GATE_MODE=enforce` (was shadow), `STRUCTURE_STOP_MODE=on` (structural stop used only when sane).
- decision service: `STRUCTURE_POLICY_MODE=full` (was shadow). Still only ever tightens a BUY NOW.
- position-stocks: `BOARD_GATE_MODE=enforce` (was auto), `BOARD_USE_MS_ENGINE=1` (was 0), new daily-structure gate `STRUCTURE_GATE_MODE=enforce`.
- All gates: no / stale / insufficient structure = no opinion (never blocks) unless `STRUCTURE_REQUIRE_DATA=true`. Thresholds are uncalibrated (WS-7 backtest still open): a wrong value costs a skipped trade, never a forced one.

## Position-stocks daily-structure gate (orders/structure_gate.py)
Looser than real-trade's (scalper buys strength): bearish daily trend / fresh bearish BOS, chase > 4.0, stretch > 5.0, room < 0.3 daily ATR.
Skip reason `STRUCTURE_BAD_LOCATION:...` (symbol-specific, so the shortlist falls through to the next pick). Snapshot stored in
`scalp_positions.structure_json`, returned as `structure` on the positions API, shown by StructurePanel on the Position Stocks tab.

## Test fixes
breakeven clamp landing on entry; 3 silent excepts in reconcile.py now log; reconcile re-arm queue reset per test; ws_client `_MAX_TICKS` test follows group341;
bulk-prefetch test checks inside the scan context; NSE fixture resets group346 pause state; `ms_passthrough.feed_keys` returns {} without a valid block
(real bug: reported a fake patched field and stamped ms_quality=unavailable).

## Not done
Gateway sniper chase gate, PREPARE refresh, StructureObservation table, backtest/calibration, chart overlays.
