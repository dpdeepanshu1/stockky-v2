# group89 (2026-10-04) - item 5: gateway Gemini cooldown after 429

Cumulative on group88. Run `bash run_tests.sh` in api-gateway on the VM, then `docker compose build api-gateway && docker compose up -d`.

## Cause
`api-gateway/main.py::_generate_ai_summary` (single-stock `/stock/{symbol}`) had no memory of a Gemini 429. While the free-tier quota was exhausted, every Analyse made a request that was certain to fail, waited for it, logged "Gemini call failed (429)" and then used the template. decision-prediction-service already cools down for 10 minutes after a 429; the gateway did not.

## Fix
- New module state `_gemini_cooldown_until` (epoch seconds). While it is in the future, `_generate_ai_summary` returns the template immediately: no HTTP request, no log line.
- A 429 starts the cooldown (`_start_gemini_cooldown`). Length is `GEMINI_COOLDOWN_SECONDS` (default 600), or the `Retry-After` header if present, clamped to 30..3600 s. A later 429 never shortens a longer cooldown.
- One warning line ("Gemini 429 for SYMBOL - skipping Gemini for Ns") and one `rate_limit_monitor` event (`source=gemini, status=429`) per cooldown window, not per request. A failing monitor is swallowed.
- Other non-200 statuses (500, 403 ...) and exceptions behave as before: warning, template, no cooldown.
- Scan paths are unaffected (they already pass `skip_gemini=True`).
- The cooldown is per process. Restarting api-gateway clears it, and the first request after a restart probes Gemini again.

## Env
`GEMINI_COOLDOWN_SECONDS` (optional, blank/invalid/<=0 -> 600). Documented in `.env.oracle.example`.

## Tests
`tests/test_main_scoring.py`: the `gemini` fixture resets the cooldown; the existing 429 test now expects the new log line (and a 500 case keeps the old "call failed" line covered); 7 new test functions (14 cases with parametrize) cover cooldown start/skip, default and Retry-After lengths and clamping, expiry, never shortening, monitor failure, one event per window, and the env parser. Real pytest 9.1.1 with the pinned requirements in a clean venv: `tests/test_main_scoring.py` 269 passed, full api-gateway suite 8047 passed.

## Not changed
- decision-prediction-service keeps its own separate cooldown (its Gemini calls are a different quota consumer in the same project, so a gateway 429 does not stop it and vice versa). Sharing one cooldown across services would need Redis; not done.
- Item 5 only covers the gateway's summary call; the 429s themselves (free-tier quota) are not something code can remove.
