# E193 - decision: the PE fallback score was not monotone (PE 45 scored better than PE 35)

Includes E192 (and E191 - E184).

## What I read
The payload short-circuit helpers in `decision/main.py` (`_derive_technical_from_payload`, `_derive_fundamental_from_payload`), the `/decide/evaluate` route, `/decide/{symbol}`
query handling and `/decide/batch`. `/decide/batch` has no caller outside the decision service (only an archived script mentions it), so nothing there was changed.

## Finding (fixed)
When a payload has a PE but no stored `fundamental_score`, the fundamental pillar is derived from the PE: 8-28 -> 62, 28-40 -> 48, 0-8 -> 55, above 50 -> 35, and everything else
(including **40-50**, negative PE and no PE) fell through to 50. So a PE of 45 scored 50 while a PE of 35 scored 48. 28 < PE <= 50 is now 48 (no new number; 50+ stays 35,
negative PE stays 50). Only payloads without a stored score and with a PE between 40 and 50 change, by -2 on the pillar.

Read and fine: the RSI buckets (the oversold bonus below 30 is deliberate), `close` / `price` / `ltp` / `cmp` fallbacks, `/decide/evaluate` argument handling.
Not changed: a loss-making company (negative PE) is neutral 50 here, which you may want lower.

## Tests
- `decision/tests/test_e193_pe_fallback_monotone.py` (14 tests): 14/14 pass on the new code, 4 fail on the unchanged E191 `main.py`. Decision suite (without `test_env_url_blank_fallback`,
  which cannot import numpy in this sandbox) passes.
- `docs/ENV_REFERENCE.md` regenerated; `--check` passes. No new environment variable.

    bash scripts/docker_test.sh decision-prediction-service
    python3 scripts/gen_env_reference.py --check
