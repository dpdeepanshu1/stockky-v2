# Session 133 — api-gateway coverage: plan + pass 1 (2026-09-29)

## Baseline (measured on the VM, 2026-09-29)
14,289 stmts, 2% — only qstash_client.py tested. (With tests/ omitted via the new .coveragerc the
production total is 14,079 stmts.) All 26 modules import cleanly in the sandbox (Python 3.12;
prod image is 3.11.9 per .python-version).

## Size by tier (production statements)
| Tier | Modules | Stmts |
|---|---|---|
| 1 tiny pure | json_safe 26, nse_holidays 10, metrics 66, price_resolver 65 | 167 |
| 2 shared/duplicated | oracle_compat 110 (byte-identical to analysis-intel + market-data copies), boot_forensics 135 (identical to position-stocks copy), kv_cache 650 (~98-line diff from analysis-intel's) | 895 |
| 3 infra-like | circuit_breaker 213, rate_limiter 340, redis_rate_limit 140, rate_limit_monitor 196, batch_worker 126 | 1,015 |
| 4 schema / store | hotpicks_schema 167, ipo_schema 137, surprise_schema 130, hotpicks_store 523, refill_additional 133, symbol_aliases 196 | 1,286 |
| 5 scanners | buy_sniper 194, instant_scanner 248, surprise_scanner 753, surprise_premarket 587 | 1,782 |
| 6 large | data_feed 1,368, ipo_scanner 1,085 | 2,453 |
| 7 main.py | 193 routes, 234 functions, 4 classes; groups: api 46, ops 25, data-feed 22, surprise 21, scan 13, stockky-hot 11, ipo 10, market 8, other ~37 | 6,403 |

## Pass schedule (one zip per pass, same layout as the upload)
1. DONE — test infra + Tier 1 (this session).
2. oracle_compat + boot_forensics (adapt the existing tests from analysis-intel / position-stocks).
3-4. kv_cache.
5. circuit_breaker. 6. rate_limiter. 7. redis_rate_limit + rate_limit_monitor. 8. batch_worker + refill_additional.
9. the three *_schema modules (in-memory SQLite). 10. symbol_aliases. 11-12. hotpicks_store.
13. buy_sniper + instant_scanner. 14-15. surprise_scanner. 16-17. surprise_premarket.
18-21. data_feed. 22-26. ipo_scanner.
27-40. main.py by route group (helpers/middleware/startup first, then api, ops, data-feed, surprise, scan, stockky-hot, ipo, market, rest).
Then: wire api-gateway into .github/workflows/service-tests.yml (needs requirements-test.txt for PyJWT).

## Conventions
- No network, no real DB/Redis: httpx/yfinance/feedparser faked; SQLite in-memory; tests/conftest.py strips
  USE_REDIS/DATABASE_URL/ORACLE_*/QSTASH_* env so nothing leaks in from the VM.
- main.py: call route functions directly or use TestClient WITHOUT `with` (the @app.on_event("startup")
  hooks at lines ~175, 8957, 8999, 9028 start background work and must not run in tests).
- Every pass reads its module line by line; real bugs get a fix + regression test and are listed in that pass's
  note. Behaviour is not changed otherwise. Coverage targets: 100% for modules 1-6 where reachable, >=90% for main.py.
- Gate: `COV_MIN=<n> bash run_tests.sh --single`; raise n every pass. Currently 0 (default).

## Pass 1 results
- New: tests/conftest.py, .coveragerc, run_tests.sh, requirements-test.txt.
- json_safe 100% (23 tests), nse_holidays 100% (14), metrics 100% (24), price_resolver 100% (38); 151 tests total, all pass.
- Observations (not changed): nse_holidays.is_nse_holiday(datetime) is silently False (all callers pass .date());
  price_resolver accepts +inf as a valid price; metrics label values are not escaped in Prometheus text;
  json_safe.sanitize leaves np.bool_ / set unchanged (not JSON-serialisable).
- Open item for you: PyJWT is not in api-gateway/requirements.txt; qstash_client.verify_signature() fail-opens
  (with a warning) when it is missing in the image. Adding it turns QStash signature verification ON in prod —
  confirm that is wanted (and that QSTASH signing keys are set) before adding it.
