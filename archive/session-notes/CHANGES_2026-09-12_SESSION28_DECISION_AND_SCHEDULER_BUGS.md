# Session 28 — decision-prediction-service + notification-scheduler-service deep audit (2026-09-12)

## Scope
Continuing into the two named areas that carry the most real-money and
real-notification risk: decision-prediction-service's actual decision logic
(`decision/main.py`, `decision/horizons.py`, re-checked `circuit_breaker.py`),
and notification-scheduler-service's scheduling logic (`run_once.py`,
`overnight_orchestrator.py`, `weekend_hydrator.py`, `symbol_master_sync.py`,
`fundamentals_batch.py`, `governance_check.py`, `main.py`). Also ran a
repo-wide AST scan for the same shape of bug found in this round (a wrapper
function/method calling itself instead of the thing it wraps), to check
whether it was an isolated incident.

## Found & fixed — three real bugs, one severe

**1. (Severe) `notification-scheduler-service/scheduler/run_once.py`'s
`_state_get()` / `_state_set()` called themselves instead of the Redis
client.** Both were meant to read/write via `_redis.get(...)` /
`_redis.set(...)` when `USE_REDIS` is on, falling back to a local `/tmp`
file otherwise. Instead, the Redis branch in each function called the
wrapper function itself (`return _state_get(key)` / `_state_set(key, raw,
ex=ex)` calling `_state_set` again) — infinite recursion, caught by a bare
`except Exception` once Python's recursion limit raised `RecursionError`,
silently falling through to the file path every single time. Because this
script explicitly runs "single-shot mode, driven by GitHub Actions cron" —
a fresh container per invocation — the `/tmp` fallback doesn't persist
across runs either. Net effect: **every dedup/state key this scheduler
relies on (`OPEN_MSG_KEY`, `CLOSE_MSG_KEY`, `SLEEP_MSG_KEY`,
`LAST_SCAN_KEY`, the `STATE_KEY` decision-change tracker, `DAILY_PICKS_KEY`)
has been resetting to "unset" on every cron tick regardless of whether
`USE_REDIS` was configured**, which is exactly the condition that produces
duplicate "market opens in 1 hour" / EOD summary messages and defeats
`should_skip_scan()`'s cooldown entirely. Fixed both functions to actually
call `_redis.get()`/`_redis.set()` (with the same bytes-decode handling
`circuit_breaker.py` already uses for Redis reads).

**2. `decision-prediction-service/decision/main.py`'s insider-selling
detector used `and` where every other equivalent check in the codebase uses
`or`.** `_extract_event_signals()`'s insider-transaction branch checked
`elif "sell" in txn_type and "sale" in txn_type:` — requiring both
substrings in the same string. Real NSE "Transaction" values are typically
`"Sale"` or `"Market Sale"` (contains "sale", never "sell"), so this could
essentially never match. Verified against
`analysis-intelligence-service/event/event_depth.py`, which checks the same
field with `"sell" in kind or "sale" in kind` in two separate places — the
correct, consistent pattern. **Insider-selling risk has never been factored
into `event_score_delta`** since this function was written; the buy-side
check right above it already correctly used `or`. Fixed to `or`.

**3. `decision-prediction-service/decision/main.py`'s `/decide/batch`
endpoint silently downgraded every actionable result to `DO NOT BUY`.**
`decide()` is a FastAPI route with `background_tasks: BackgroundTasks =
None` — that default is only overridden by FastAPI's own dependency
injection when a request comes in through the ASGI cycle. `decide_batch()`
calls `decide()` directly as a plain coroutine (`await decide(sym,
force=force)`), bypassing that injection entirely, so `background_tasks`
stayed `None`. `_decide_impl()` unconditionally calls
`background_tasks.add_task(...)` whenever the decision is `BUY_NOW` /
`PREPARE_TO_BUY` with a valid close price — which raised `AttributeError`
on `None`, caught by `_decide_impl`'s own broad `except Exception` block,
which returns the `DO_NOT_BUY` / confidence `"Low"` fallback payload
instead of the real decision. **Every actionable result produced via
`/decide/batch` was silently downgraded to `DO NOT BUY` before reaching the
caller** — the opposite of the endpoint's own docstring ("Uses the same
/decide logic... Does not change scoring"). Fixed by constructing a real
`BackgroundTasks()` per symbol and running it explicitly after `decide()`
returns (there's no ASGI response cycle here to run it automatically), which
also fixes the secondary effect that `record_prediction_for_training` was
never actually being called for batch-decided symbols either.

## Checked and confirmed correct (no bug found)
- `decision/horizons.py` — full read; multi-horizon weighting, regime
  multipliers, closed-loop win-rate threshold shifts, and the
  per-horizon-copy fix for the pillars-dict mutation bug (already
  documented in-file from a prior session) are all correct.
- `decision/circuit_breaker.py` — full read (re-confirmed from session 27);
  the half-open/`opened_at` restart-guard fix it documents is correctly
  implemented.
- `scheduler/governance_check.py`, `overnight_orchestrator.py`,
  `weekend_hydrator.py`, `symbol_master_sync.py`, `fundamentals_batch.py`,
  `scheduler/main.py` — full read, no bugs found. `symbol_master_sync.py` in
  particular already documents four of its own past fixes ("Bug L fix"
  parts 1-4) and the `bindparam(expanding=True)` / minimum-plausible-universe
  guards are implemented correctly.
- One design gap noted but **not fixed** (not a bug, a documented-but-never-
  wired feature): `weekend_hydrator.py` defines `_PRIORITY_SECTORS`
  ("Symbols from these sectors will be hydrated in the first batch pass")
  but never uses it — `hydrate_batch()` slices `all_symbols` in plain
  alphabetical order with no sector-priority reordering. Implementing this
  properly needs sector data per symbol (from `symbol_master`, a different
  table/service) that isn't available at the point `_fetch_universe()`
  returns bare symbol strings, so this wasn't a quick fix — flagging for a
  deliberate decision rather than a speculative half-implementation.
- Repo-wide AST scan for "function calls itself by name" (the same shape as
  bug #1 above): several hits in `json_safe.py`/`models.py`/`app.py`/
  `train.py`/`market-data-service/main.py` are legitimate recursive tree
  sanitizers (recursing into nested structures, not into themselves with the
  same call). `api-gateway/redis_rate_limit.py`'s `LocalMemoryRateLimiter`
  class methods calling same-named module-level functions are also a false
  positive (method calling the free function, not calling itself). No
  further instance of the actual bug found.

## Honest scope remaining
Still not audited: the bulk of api-gateway's scanner internals
(`ipo_scanner.py`, `surprise_scanner.py`, `data_feed.py`,
`hotpicks_store.py`, `instant_scanner.py`, `buy_sniper.py`),
`notification-scheduler-service/notification/main.py` (36K, the actual
notification-sending logic, as opposed to the scheduling logic covered this
round), decision-prediction-service's `training/` and `prediction/`
subtrees (model training/walk-forward, tens of thousands of lines), and the
frontend (~22.5k lines).
