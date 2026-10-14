# Group 369 - drift fixes and safe rollout defaults
- Re-synced `stockky_ms/` (MANIFEST was missing backtest/observation/replay/candles); `--check` green.
- Regenerated `docs/ENV_REFERENCE.md`; `--check` green.
- `docker-compose.yml`: decision-prediction `STRUCTURE_POLICY_MODE` default full -> shadow.
- `.env.example`: active block setting policy, real-trade gate (REAL+DEMO), sniper gate and structural stop to `shadow` (plan stage R1).
  Python code defaults are unchanged (tests pin them); the env values override. Rollback to live behaviour = delete those lines.
- Added `docs/GROUP339_BASELINE.md` (honest run record).
Still open: real backtest on history, decision-service reject observations, T+10/T+20 evaluator, scheduling the two jobs, StructureEdgeCard wiring.
