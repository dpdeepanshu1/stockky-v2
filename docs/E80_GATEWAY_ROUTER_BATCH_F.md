# E80 - gateway router split, batch F (2026-10-23)

11 more handlers (16 route registrations) moved out of `services/api-gateway/main.py` into `gateway_support/bound_routes_f.py`, same MainRef method as E55 / E76-E79. 49 handlers remain in `main.py` (60 before this batch, by E78's count).

Moved (bodies unchanged except `name` -> `ref.name`): `ops_idle_tick`, `health`, `wake_all_services_probe` (`POST /wake/all`), `run_scan` (`GET /scan`), `get_market_indices`, `clear_universe_cache`, `catalyst_alert_status`, `ops_keepalive` (GET + POST), `refresh_prepare_to_buy` (3 routes), `data_feed_status` (2 routes), `purge_over_cap_feed_symbols` (2 routes).

Selection rules (same as before): plain `@app.<method>("<literal path>")` decorators only; parameters only `str` / `int` / `bool` / `float` with literal defaults (no main.py name in a signature, so `from __future__ import annotations` stays); no nested function / lambda / class; no `global`; no name that is both a main.py global and a local; defined after the factory build line. 13 handlers passed; 2 were left out on purpose:
- `data_feed_structure_refresh_status` and `data_feed_symbol`: `tests/test_group360_structure_refresh.py` finds these routes by scanning `main.py` decorators (`_routes()`) and asserts `MAIN_SRC.index('@app.get("/data-feed/{symbol}")')`. Moving them needs that test rewritten to read include order instead; do it together with the `dependencies=[Depends(...)]` batch, which needs the same kind of test change (`test_admin_guard.py`).

## Checked here
- Route table: all 163 route registrations across main.py and the six factories are identical to the E78 zip, in order, with each router expanded at its include site.
- Bodies: the AST of each moved function (decorators aside) equals the E79 original once `ref.` is removed, docstrings and constants included. Text is moved byte-for-byte (comments kept; lines inside multi-line strings not re-indented).
- Name completeness (static proof, `symtable`): in each moved handler every free global is either a builtin or `ref.`-prefixed, and the set of `ref.<n>` names equals the set of main.py globals the original used. The same check passes for the 16 handlers of E79, and fails on a deliberately broken copy (one `ref.` removed).
- Guard tests, stand-in runner (no pytest here): new `tests/test_gateway_support_split_f.py` plus the existing split tests, 24 passed, 8 skipped (live-app tests, need fastapi). `py_compile` ok. No existing test reads these handlers from `main.py` source (searched for the handler names and the route paths).
- Differential fuzz (supplementary, weak): old and new handlers run against the same seeded auto-generated fakes, comparing results, exceptions and the ordered call log: 2,400 comparisons, 0 differences. A mutation test of that harness (swap one `ref.<n>` for another name) killed only 29 of 76 mutants, because the random fakes often make a handler exit early; so treat this as a smoke test, not proof. The static proof and AST equality above are the real evidence.

## NOT run here
pytest / fastapi / httpx are not installable in the sandbox: the existing gateway suites and the live-app tests did not run. On the VM: `cd services/api-gateway && bash run_tests.sh`, rebuild api-gateway, then try `GET /health`, `GET /scan`, `GET /market/indices`, `GET /ops/keepalive`, `POST /ops/keepalive`, `GET /data-feed/status`, `GET /catalysts/alert/status`, `POST /ops/idle-tick`, `POST /wake/all`; compare `/openapi.json` before and after (the `force_refresh`, `lite` and `deep` query parameters should be unchanged). Roll back = redeploy the E79 zip.

## Next
What remains: routes with `dependencies=[Depends(...)]` (about 12) and the two held back above (all three need test changes), nested-function handlers (about 17), pydantic-model parameters such as `WatchlistUpdate`. These need hand-written moves.
