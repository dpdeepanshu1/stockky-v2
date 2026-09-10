# 2026-09-09 (session20) — Gate 6 composite floor unreachable for
# non-UPPER_CIRCUIT candidates — applied

## Symptom
30+ risk-approved REAL candidates across the whole session (12:57pm–3:25pm),
zero entries. Composite scores clustered at 40.0–47.5, always below the 50
floor. Included GRAPHITE (VOLUME_SHOCK_HIGH_CONVICTION, +14.75% on the day —
the single strongest mover in the batch) stuck at composite 47.5 — WAIT.

## Root cause
`_atr_stop_target_pct()` (entry_engine/entry.py) fixes reward:risk at
exactly 2.0:1 (ATR case) or 2.03:1 (flat fallback) for every candidate, by
construction — entry/stop/target are all anchored to the same `entry_price`
(2026-09-01 tick-size fix), and `target_pct = stop_pct * (3.0/1.5)` always.
Since `MIN_REWARD_RISK_RATIO` is also 2.0, the composite's `rr_norm`
sub-score is ~0 for virtually every candidate that ever reaches Gate 6 —
never higher, regardless of setup quality. The RR term was carrying 35% of
the composite while contributing nothing discriminating, which made the
50-point floor structurally unreachable for anything below HIGH_CONVICTION.

## Fix applied this session
1. `config.py` — `ENTRY_COMPOSITE_WEIGHT_CONVICTION/RR/DRIFT` rebalanced
   `0.50/0.35/0.15` → `0.65/0.10/0.25` (RR kept nonzero, not zero, in case a
   future stop/target redesign — e.g. technical-level-based targets — makes
   RR genuinely variable again).
2. `entry_engine/entry.py` — Gate 6's WAIT message now states the actual
   reason for that specific candidate ("below the N composite floor" vs
   "not among this cycle's top N — cleared the floor but ranked below
   stronger setups"), instead of an ambiguous "or" for every skipped row.
3. `candidate_engine/candidates.py` — corrected the stale comment above the
   HC/UC/base conviction-score elevation block (previously assumed RR could
   reach ~3.0 for a base-tier pass; it structurally can't) to reflect the
   real fixed RR and the new weight math.
4. **New this session** — `candidate_engine/candidates.py`'s
   `_recently_candidated_symbols()` + `config.ENTRY_GATE6_REQUEUE_MINUTES`
   (default 15): a candidate WAIT'd *only* by Gate 6 (risk-approved, just
   not in this cycle's top `ENTRY_MAX_NEW_PER_CYCLE`, or below the composite
   floor) is marked `consumed=True` immediately, same as a real gate-1-5 /
   risk_engine WAIT or an ENTER — so without this fix it was silently
   excluded from re-candidacy for the full 6h/2h dedupe cooldown, directly
   contradicting Gate 6's own message that it's "re-evaluated fresh next
   cycle." Now a pure Gate 6 skip becomes re-eligible after
   `ENTRY_GATE6_REQUEUE_MINUTES` instead of the multi-hour window. Gate 1-5
   / risk_engine WAITs and real ENTERs are unaffected — they keep the full
   cooldown, which is what actually prevents duplicate-row spam. Fails safe
   (keeps the full cooldown) if the extra lookup errors.

## Sanity check (reconstructed from this session's own pasted dashboard data)
Backed out each WAIT row's drift-safety sub-score from its OLD-formula
composite, then recomputed under the new weights: **all 35 risk-approved
WAIT rows from today would now clear the 50 floor**, GRAPHITE included
(47.5 → 65.4). `ENTRY_MAX_NEW_PER_CYCLE=3` still caps actual entries per
cycle by design — this only fixes the floor being unreachable, it does not
remove the intentional per-cycle concentration limit.

## Follow-up audit (same session) — 2 more fixes
5. **Defense-in-depth**: the new `_recently_candidated_symbols()` join
   didn't filter `decision_type == "ENTRY"` / `candidate_id.isnot(None)`
   explicitly — `main.py`'s own dashboard query does, precisely to avoid a
   future decision type (the model comment lists `"EXIT"` as valid, though
   nothing writes one with `candidate_id` set today) silently joining in.
   Matched that existing convention.
6. **Real bug, not just defensive**: the new gate6-skip comparison
   (`created_at < gate6_cutoff`) runs in **Python**, unlike every other
   cutoff filter in this file, which runs inside the SQL query itself.
   `TradeDecision.created_at` is a plain `Column(DateTime)` (no
   `timezone=True`), and this service runs against both Oracle Autonomous
   DB and Postgres (Neon) — depending on dialect, a value written as
   tz-aware UTC can come back from a `SELECT` with its tzinfo silently
   stripped. Comparing that naive value against the tz-aware
   `gate6_cutoff` would raise `TypeError: can't compare offset-naive and
   offset-aware datetimes` at runtime — a crash `python -m py_compile`
   can't catch, since it's a type error, not a syntax error. Fixed by
   normalizing both sides to naive UTC before comparing (both were UTC to
   begin with, so stripping tzinfo where present is safe). Verified with a
   standalone simulation of naive-old / aware-old / naive-new timestamps
   against the cutoff — correct result in all three cases.

## Follow-up audit round 2 (same session) — consistency fix + real import verification
7. **Consistency fix**: my own naive/aware fix from round 1 (stripping
   tzinfo on both sides) worked, but reinvented a wheel this codebase
   already has — `tz_utils.py` defines `as_aware()` specifically for this
   exact crash class (its own docstring: this already took down
   `GET /status/REAL` and `POST /dhan/connect` before the helper existed),
   and 7 other modules in this service already import and use it. Replaced
   the ad-hoc tzinfo-stripping with `from tz_utils import as_aware`,
   matching the codebase's own established convention instead of adding a
   second, only-locally-reasoned way of handling the same bug class.
8. **Verification upgrade**: `python -m py_compile` only catches syntax
   errors — it would not have caught the naive/aware TypeError from finding
   #6 above (that's a runtime type error). Installed the service's actual
   dependencies (httpx, sqlalchemy) in the sandbox and ran a **real Python
   import** of `candidate_engine/candidates.py`, `config.py`, and
   `entry_engine/entry.py` — all three import cleanly end-to-end, confirming
   `from tz_utils import as_aware` resolves correctly against this
   package's layout, not just that the file parses.

## Round 3 — market_is_open hardcoding + auto_pilot event-loop isolation (2026-09-10)

### 9. Hardcoded `market_is_open=True` wired to the real check
`entry_engine/entry.py`'s `_account_state()` already called
`tz_utils.is_market_open_ist()`. `manual_engine.py` and `main.py`'s
`/cycle/risk-check` dry-run route both still had `market_is_open=True`
hardcoded, with comments claiming this was a shared "Phase-3 TODO" — that
claim was stale for `manual_engine.py` (entry_engine already had it wired)
and simply inaccurate as a blanket statement. Traced the actual effect
before wiring it in: `risk_evaluate()` (which reads this field) is only
ever called from `manual_engine.py`'s **BUY** path — manual SELL never
constructs an `AccountState` or calls `risk_evaluate` at all (confirmed via
grep — SELL bypasses it entirely, matching the module's own documented
design). So wiring in the real check only affects manual BUY confirmations,
exactly matching `entry_engine`'s existing automatic-BUY behavior — no
conflict with the "never block an exit" design principle. Also confirmed
`risk_engine/engine.py`'s own docstring explicitly designs check #2 (market
hours) to be one of only two checks that intentionally still apply even to
a SELL that *does* reach `risk_evaluate()` — it's an exchange-hours
reality, not a risk-policy gate. `offline_test_harness.py`'s synthetic DEMO
account left at `market_is_open=True` on purpose (deterministic offline
test output) — not a bug, not touched.

### 10. auto_pilot.py event-loop isolation (decision #28's deferred fix)
`_full_tick`, `_exit_only_tick`, and `_schedule_tick` all ran their entire
body — session creation through every db.query/commit, plus their own
awaited network calls — directly on the shared main asyncio event loop
that also serves `/health` and every other route. A slow cycle could block
that loop long enough to fail the healthcheck window despite the process
being alive — decision #28's diagnosed root cause, previously only
mitigated by widening the healthcheck tolerance.

**Fixed**: each tick's body now runs on a dedicated worker thread with its
own fresh event loop (`asyncio.run` inside `asyncio.to_thread`). The
blocking risk this was deferred over — `asyncio.Lock` is bound to the loop
that created it and isn't safe to acquire across threads/loops — is
resolved by switching the per-mode mutual-exclusion lock from
`asyncio.Lock` to `threading.Lock`, which has no such binding. Each tick's
original acquire semantics are preserved exactly: `_full_tick` still WAITS
for the lock (blocking `with lock:` now happens on the worker thread, never
the main loop); `_exit_only_tick` and `_schedule_tick` still SKIP if busy
(non-blocking `lock.acquire(blocking=False)`, closing a narrow TOCTOU gap
the original `if lock.locked(): return` + separate acquire had, as a side
effect of the rewrite).

**What was checked before this change** (code-audit confidence, no live
stack access from this sandbox):
- This file's `asyncio.Lock` was the *only* module-level asyncio.Lock/
  Event/Queue anywhere in real-trade-service (grepped the whole service).
- No module holds a persistent, shared `httpx.AsyncClient` at module
  scope — every call site uses `async with httpx.AsyncClient()` fresh per
  call, so no loop-bound HTTP client for a worker thread's own loop to
  collide with.
- `db.py`'s engine is a standard `create_engine()` connection pool,
  explicitly designed for concurrent checkout from multiple threads — each
  tick already creates its own fresh `Session()` per call, so running that
  on a different OS thread each time is the same access pattern SQLAlchemy
  is built for.
- The three loops (`_fast_exit_loop`/`_full_cycle_loop`/`_schedule_loop`)
  still iterate `for mode in ("DEMO","REAL"): await <tick>(mode)`
  sequentially, and `await asyncio.to_thread(...)` still blocks until the
  thread finishes — DEMO and REAL still never run concurrently *within*
  the same loop, unchanged from before.

**Known residual, not fixed here**: the three loops already ran
concurrently *with each other* pre-existing (that's the point of the
decoupled-cadence design) — cooperatively interleaved on one loop before
this change, real OS-thread concurrency after it. The one place this could
matter: `entry_engine/entry.py`'s module-level `_regime_cache` (2-minute
TTL cache, shared across modes, no lock of its own) — two full-cycle ticks
for different modes landing at the same instant on different threads could
both see it stale and both re-fetch, last-write-wins. Benign (duplicate
fetch, not a sizing/order-placement correctness issue) and not addressed
in this change.

**Verified**: real Python import of `execution/auto_pilot.py` (and every
other file touched this session) against the service's actual dependencies
succeeds. Also wrote and ran a standalone functional test of the exact
lock+thread-offload mechanism in isolation (no DB dependency) — 4/4 checks
passed: main loop stays responsive during a simulated slow tick; same-mode
skip-if-busy works; different-mode ticks never block each other; DEMO-then-
REAL sequential ordering is preserved.

**Separately discovered, NOT fixed (out of scope for this round)**: the
module's own docstring claimed the lock "prevents the manual 'Run Cycle'
button + auto-pilot timer from racing." `main.py`'s `/cycle/run/{mode}`
route calls `cycle_runner.run_cycle_core` directly and never acquires
`_get_lock(mode)` at all — the manual trigger and the auto-pilot loops can
race today, contrary to what the docstring said. Not touched here (separate
bug from what was asked this round); flagged for a future session.

**NOT LIVE-TESTED against the real stack** — bigger, higher-stakes change
than a config tweak. Recommend running this in DEMO first: confirm
"Auto-pilot FULL CYCLE loop running" ticks still fire normally, `/health`
stays responsive during/after a busy cycle, and no cycle silently double-
runs for a mode (check `TradeOrder` rows for duplicates) before trusting
this with REAL capital.

## Not verified live
Reasoned from code + this session's pasted dashboard data and a standalone
recomputation, same as the earlier same-day version of this fix. No network
access in this sandbox to market-data, Dhan, or the DB. **Run through a
PAPER-mode cycle before this reaches REAL** — confirm entries actually fire,
land at sane sizes, and that the Gate-6-requeue change doesn't reintroduce
duplicate candidate rows for a still-qualifying symbol.

## Files changed
- `services/real-trade-service/config.py` — composite weights + new
  `ENTRY_GATE6_REQUEUE_MINUTES`
- `services/real-trade-service/entry_engine/entry.py` — Gate 6 WAIT reasoning
- `services/real-trade-service/candidate_engine/candidates.py` — comment
  fix + `_recently_candidated_symbols()` requeue-shrink logic
