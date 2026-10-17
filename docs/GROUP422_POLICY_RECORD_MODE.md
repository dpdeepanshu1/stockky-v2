# Group 422 - record-only path for the decision structure policy

## Why
Group 370 made the structure policy always live and removed shadow mode. The side effect: a new or stricter rule could only be tested by
turning it on for real money. This restores a measure-first path without changing any default.

## Behaviour
| Setting | Effect |
|---|---|
| (nothing set) | unchanged: policy live, nothing shadowed |
| `STRUCTURE_POLICY_MODE=record` | every rule is evaluated and logged, none applied; legacy stop / target kept; no P9 upgrade |
| `STRUCTURE_POLICY_SHADOW_RULES=P7,P8` | those rules are record-only, every other rule stays live |
| `STRUCTURE_POLICY_MODE=off` | kill switch, unchanged (shadow list ignored) |
| `shadow` / `caps` / anything unknown | still means live (deliberate, so stale env files never silently disable the policy) |

Rule matching: exact id (`P1_NO_STRUCTURE`) or a prefix up to an underscore (`P8` matches `P8_NO_BOS`; `P` or `P2_CH` match nothing).

## What is recorded
- `structure.shadow_rules`: rules that fired but were not applied. `structure.would_be_decision`: what ALL rules would have produced.
- `rules` / `policy_decision` / `applied` still describe only what was applied.
- Observation hook: `rules` gets `shadow:<id>` entries, `features.would_be` the would-be decision, `policy_mode` = `record` in record mode.
- A record-only rule that fired still blocks a P9 PREPARE -> BUY NOW upgrade, and an upgrade only happens in full mode.

## Caution
Shadowing a rule LOOSENS live gating (that rule stops downgrading BUY NOW). Use it to evaluate a rule you are about to add, or to
measure P7 / P8 on purpose, not as a default. The real-trade `structure_gate` and the sniper gate are separate and unaffected.

## Verification
`decision/tests/test_group422_policy_record_mode.py` (14 tests), the existing 36 policy / wiring tests, frontend `npm test`.
