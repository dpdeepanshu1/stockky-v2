# E78 - gateway router split, batch D (2026-10-23)

11 more handlers (22 routes) moved out of `services/api-gateway/main.py` into `gateway_support/bound_routes_d.py`, same MainRef method as E55 / E76 / E77. 76 handlers remain in `main.py` (87 before this batch).

Moved (bodies unchanged except `name` -> `ref.name`): `ops_qstash_tick`, `scan_batch`, `api_run_premarket_feed`, `api_surprise_repair_batch`, `api_ipo_notify_top_picks`, `stockky_hot_endpoint`, `data_feed_update_single`, `data_feed_update_batch`, `data_feed_update_batch_refresh`, `data_feed_resume`, `stockky_hot_repair_batch`.

## What is different from batches B and C
These handlers have `Request`, `BackgroundTasks`, `Query(...)` or `Optional[...]` in their signature. Those are main.py names, and the factory runs at import, so:
- the signature reads `request: ref.Request`, `limit: int = ref.Query(15, ge=1, le=100)`, `symbol: ref.Optional[str] = ref.Query(None)`, `background_tasks: ref.BackgroundTasks` and is evaluated when the factory is built, exactly when the original decorators evaluated it. All four names are imported at the top of main.py, before the build line.
- `bound_routes_d.py` therefore has NO `from __future__ import annotations` (with it FastAPI would look the annotation strings up in the factory module's globals, fail to find `Request`, and treat the parameter as a query field). A source test pins this.
- Only framework / typing names were allowed in a signature. Handlers whose signature uses a model class defined in main.py (`WatchlistUpdate`) or `WebSocket`, `dependencies=[...]` decorators, nested functions, lambdas or `global` were not touched.

## Existing guard test changed
`tests/test_gateway_support_split.py` rule 2 treated `from data_feed import extract_feed_payload` INSIDE a handler as a use of the main.py name `extract_feed_payload` (main.py also imports that name). A name a handler imports or assigns itself is a local, so the rule now skips those. It still flags any real bare main.py name, and every `ref.<name>` must still exist in main.py.

## Checked here
- Route table: all 163 route registrations across main.py and the four factories (E55 / E76 / E77 / E78) are identical to the E77 zip, in order, with each router expanded at its include site.
- Bodies and signatures: identical to the originals once `ref.` is removed (docstrings compared after `inspect.cleandoc`; no triple-quoted string other than a docstring inside a moved body).
- Names: all `ref.<name>` exist in main.py; the factory is built once, before the first batch-D include.
- Behaviour: original (exec'd from the E77 main.py source) and moved handlers run side by side under fakes: 11 handlers x their argument sets x 68 world variants (QStash signature ok / bad / crashing / client absent, body unreadable / not JSON / each action, request.json failing, every payload shape for the feed updates, upstream up / down / 429 / 503 / 404, price over the cap, save failing for one symbol, IPO list empty / failing / import failing, Telegram delivered / not / bad JSON, hot cache warm / stale / cold, background warm-up failing, hotpicks store failing / missing, job running / idle), each called twice with the logger patched between the calls. 1,280 comparisons, 0 differences. A deliberately broken copy is caught by the same script (126 and 4 differences for the two planted bugs I tried).
- `tests/test_gateway_support_split_d.py` (new) and the existing split tests pass in a stand-in runner (20 passed; the 4 skipped are the live-app tests that need fastapi); `py_compile` ok.

## NOT run here
pytest / fastapi / httpx are not installable in the sandbox, so the existing gateway test files and the live-app tests did not run. This batch is the first one that changes signatures, so the live-app test in `test_gateway_support_split_d.py` matters most: it checks that `request`, `background_tasks` and the `Query` limits resolve to the real FastAPI objects. On the VM: `cd services/api-gateway && bash run_tests.sh`, rebuild api-gateway, then try `POST /scan/batch` with `{"symbols":["RELIANCE"]}`, `POST /api/feed/update`, `POST /data-feed/resume`, `GET /stockky-hot`, `POST /api/surprise/repair-batch?limit=5` and `GET /openapi.json` (the `limit` / `symbol` query parameters and the request bodies should look the same as before). Roll back = redeploy the E77 zip.

## Next
~33 more handlers have no blocker by the same checks, but several take a pydantic model or `Depends` and need a hand-written move; `dependencies=[Depends(...)]` routes (about 12) and the ones with nested functions (about 17) are the remainder. One batch at a time, each through the VM tests.
