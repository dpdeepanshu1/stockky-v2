# Group 400 - Buy Sniper card shows the structure plan

The sniper card had only a badge ("Structure OK / warns") with chase and R:R in a tooltip. It now also shows, under "Stop loss":

| Row | Source | Shown when |
|---|---|---|
| Structure stop | `ms_stop` | a positive level exists |
| Structure target (R:R) | `ms_target`, live R:R | a positive level exists |
| Entry zone | nearest bullish, unfilled order block or fair-value gap whose bottom is not above the price | one exists; never invented |
| Wait for entry | first structure reason, or chase >= `STRUCTURE_WAIT_CHASE_ATR` | the setup is extended or the check warns |

Everything is display only. The card's own `stop_loss`, `target_price`, `reward_risk_ratio`, the gate verdict (`would_reject`, `enforced`) and which cards surface are unchanged.

- Gateway: `buy_sniper.structure_verdict` adds `entry_zone`, `wait_for_entry`, `wait_reason`; `build_suggestion` copies them into `card["structure"]`.
- Frontend: `structureView.ts` `sniperPlanRows`, rendered in `BuySniperModal.tsx`. Older servers send no such fields, so no extra rows appear.
- Env: `STRUCTURE_WAIT_CHASE_ATR` (default 0.75; 0 = never say wait).
- Tests: 7 in `services/api-gateway/tests/test_group357_sniper_structure_gate.py`, 1 block in `frontend/src/structureView.test.mjs`.
- Not run here: pytest (not installed), `npm run build` / `tsc` (no node_modules). The plain-runner gateway file and the node test pass.
