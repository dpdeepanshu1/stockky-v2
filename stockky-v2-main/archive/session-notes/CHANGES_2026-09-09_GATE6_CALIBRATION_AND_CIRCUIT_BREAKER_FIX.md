# 2026-09-09 — Gate 6 Calibration Fix + Api Gateway Circuit Breaker Auto-Reset

## Problem 1: Gate 6 blocking ALL volume-shock entries

### Symptom
`evaluated=20, entered=0, waited=20` — every candidate WAIT'd with:
> "Risk-approved (composite quality score 40.0/100) but not among this cycle's top 3
> candidates, or below the 50 composite floor."

Even UPPER_CIRCUIT stocks (Graphite India +16.65%, Wheels India +10.78%, Astec Lifesciences
+15.29%) visible in Groww were being blocked.

### Root cause
Gate 6 (2026-09-08) uses composite score = conviction(50%) + rr(35%) + drift(15%).
The old conviction scores were too low for this weight:
- UC  (69.7% win): conviction=75 → composite ~45 at typical RR=2.0, mid drift → BLOCKED
- HC  (55.7% win): conviction=65 → composite ~47 at RR=2.0, mid drift → BLOCKED
- Base (48.1% win): conviction=55 → composite ~42 at RR=2.0 → BLOCKED

The floor (50) was unreachable at any realistic RR/drift combination for UC and HC.
Gate 6 was silently cancelling every entry the system was designed to make.

### Fix — 3 changes

**1. `candidate_engine/candidates.py` — raise conviction scores:**
- UC  tier: 75 → 85  (composite ~57 at RR=2.0, mid drift ✅)
- HC  tier: 65 → 75  (composite ~52 at RR=2.0, mid drift ✅)
- Base tier: 55 → 60  (composite ~50 at RR=3.0, fresh drift ✅)

**2. `entry_engine/entry.py` — UC bypasses composite floor in Gate 6:**
UPPER_CIRCUIT candidates (69.7% backtest win rate) skip the floor check entirely.
Their signal quality is established by the backtest, not by the RR/drift blend.
They still count toward ENTRY_MAX_NEW_PER_CYCLE and sort first (score=85).

**3. `entry_engine/entry.py` — store `is_upper_circuit` flag in approved_entries:**
Reads `cand.decision_label == "VOLUME_SHOCK_UPPER_CIRCUIT"` — no model changes needed.

## Problem 2: Api Gateway Half-open (18/3 failures, 29/3 failures)

### Symptom
Signal Sourcing Health shows "Api Gateway: Half_open, 29/3 failures · retry in 0s"
and "Market Data: Closed" — the circuit breaker opened on cold-start/timeout errors
and kept itself partially open across container restarts, permanently degrading
candidate signal quality.

### Root cause
Circuit breaker state is in-memory per container. When the container restarts (deploy,
OOM, scheduler restart), the breaker starts closed (correct). But when market-data
calls time out at startup (cold start, free tier sleep), the breaker opens and enters
half-open. The "retry in 0s" shows it tries to recover but each test probe also times
out → re-opens → permanent half-open loop.

### Fix — `api-gateway/main.py` startup event:
Added `reset_all_breakers()` call in `_start_shared_http()` at every startup.
This clears any stale open/half-open state from the previous run so the breaker
starts clean. Breakers re-open normally if the downstream is genuinely down after
startup — the reset only prevents carry-over of startup timeout storms from
persisting into steady-state operation.

## Files changed
- `services/real-trade-service/candidate_engine/candidates.py` — conviction scores
- `services/real-trade-service/entry_engine/entry.py` — Gate 6 UC bypass + flag
- `services/api-gateway/main.py` — startup circuit-breaker reset

## What this does NOT change
- Gate 6 is still active for non-UC candidates (composite floor=50 still applies)
- ENTRY_MAX_NEW_PER_CYCLE=3 still limits entries per cycle
- All gates 1-5 and risk_engine still apply as before
- Circuit breaker thresholds unchanged (failure_threshold=12, recovery=30s)
