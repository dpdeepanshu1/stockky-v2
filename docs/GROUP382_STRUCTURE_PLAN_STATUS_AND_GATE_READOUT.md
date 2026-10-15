# Group 382 - status against STOCKKY_STRUCTURE_UPGRADE_EXECUTION_PLAN, and a gate readout

## What was added
`scripts/structure_gate_check.py` (+ `scripts/test_structure_gate_check.py`, 9 tests, wired into `service-tests.yml`). It reads
- one `ab_report.json` per seed from `scripts/ms_ab_report.py` -> **G2 (benefit)**: every sample must have verdict `pass`, the policy must keep >= 30% of baseline
  signals, no regime with >= 30 candidate signals may have a negative edge; fewer than 2 samples = `NOT_ENOUGH_DATA`; rules that do better when dropped are noted.
- the JSON of `GET /api/structure/report` -> **G4 (live)**: >= 20 observed sessions, >= 30 fully scored rows in both the passed-BUY-NOW and the demoted group,
  passed hit-rate and 20-day return not below the demoted group; a P9 note if upgraded PREPAREs did worse than held ones.
```
python3 scripts/structure_gate_check.py --ab seed1/ab_report.json --ab seed2/ab_report.json --live report.json [--strict]
curl -s http://<decision-prediction-host>:<port>/api/structure/report > report.json   # your real host and port
```
Verdicts are PASS / FAIL / NOT_ENOUGH_DATA / NO_INPUT. It changes nothing. No code in any service was touched.

## Plan status (what is left)
1. **Promotion gates**: shadow was removed in group 370, so the rules are live without G2/G4 having passed. Latest evidence: 346-symbol A/B `no_edge`; 1 live observation.
   Use the readout above; keep `STRUCTURE_POLICY_MODE=off`, `STRUCTURE_GATE_MODE=off`, `STRUCTURE_STOP_MODE=off`, `STRUCTURE_EXIT_MODE=off`, `STRUCTURE_PREPARE_UPGRADE=0` ready.
2. **Structure exit**: default `STRUCTURE_EXIT_MODE=tighten` (ratchet only: raises the stop of a profitable position, never lowers it, never touches a losing one).
   Selling happens only when you set `exit`.
3. **P9 PREPARE -> BUY NOW upgrade** is on by default (`STRUCTURE_PREPARE_UPGRADE`, plan said default off). It requires every BUY NOW check to pass; its own report is `prepare_upgrade`.
   Left unchanged because group 370 turned it on deliberately.
4. **WS-7**: walk-forward calibration on real data and a candidate `STRUCTURE_PARAMS_JSON` are not produced yet; thresholds are still the plan's starting values.
5. **G1**: corporate-action segmentation on >= 10 real splits/bonuses, truncation invariance on real data and the `/analyze` p95 latency check are unverified.
6. **WS-2**: `position-stocks/screening/market_structure.py` keeps its legacy bodies; the engine is behind `BOARD_USE_MS_ENGINE` (default 0, legacy fallback). Golden test: group 365.
7. **WS-0 baseline**: frontend build, analysis coverage gate and Python 3.12 not recorded.
8. **WS-14 (deferred by the plan)**: ML features v2, weekly / intraday structure, Wyckoff, volume profile: none exist.
