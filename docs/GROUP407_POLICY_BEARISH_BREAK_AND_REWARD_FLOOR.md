# Group 407 - decision policy: bearish-break demotion and reward floor

Two rules the structure plan asked for were missing from `decision/structure_policy.py` (checked against the code, not the notes). Both only make a
decision MORE conservative; neither upgrades anything. Both read the `ms_*` summary the policy already receives.

| Rule | Fires when | Effect |
|---|---|---|
| `P5_BREAK` | the daily trend is NOT bearish (that is `P5_TREND`) and there is a fresh, not-failed bearish BOS, or a bearish CHOCH, within `bearish_break_bars` (5) bars, and no bullish CHOCH came after it | BUY NOW -> WAIT; a PREPARE is re-checked too (hard rule). Same 5-bar window as the real-trade gate. |
| `P4_REWARD` | the structural target is less than `reward_pct_min` (2.5) % above the live price | BUY NOW -> PREPARE TO BUY (it also blocks the P9 upgrade, which needs every rule clear) |

Both thresholds live in `STRUCTURE_POLICY_PARAMS_JSON` (`{"bearish_break_bars": 0}` or `{"reward_pct_min": 0}` switches one off). The engine's own
`target_below_objective` flag uses 5 %, which is not enforced anywhere; 2.5 % here is the figure from the plan review, not a calibrated value, and the
A/B (`scripts/ms_ab_report.py`) replays the gate rules only, so neither rule has been validated on history. Watch `GET /api/structure/report` and the
per-rule counts in the scan panel (labels added in `frontend/src/structureView.ts`, which also gained the missing P7_LOCATION / P8_NO_BOS / P8_OLD_BOS /
P1_NO_STRUCTURE labels).

Tests: `decision/tests/test_group353_structure_policy.py` (+5; one P8 case changed - a FRESH bearish BOS is now P5_BREAK / WAIT, an old one is still
P8_NO_BOS) and `frontend/src/structureView.test.mjs`.
Not done here (needs the replay extended first, they change which entries go live): the P8 retest branch, the independence count in `scoring.py`.
Also corrected: `docs/GROUP339_MARKET_STRUCTURE_ENGINE.md` said `STRUCTURE_EXIT_MODE` defaults to `tighten`; the default is `exit` since group 399.
