# position-stocks-service — Implementation Status

> **How to use this file:** When starting a new session, read this file + TRACKING.md
> to understand exactly where work left off. The §Next step section tells you the
> single thing to do next — no re-derivation needed.

---

## Session 20 (this session) — confirmed auto-pilot is admin-session-independent (matches real-trade-service); deeper audit; 1 new visibility gap fixed

**Requested check: does admin session expiry/logout stop Auto-Pilot?** No —
verified line-by-line and confirmed already correct, matching
real-trade-service's tested reference behavior
(`main.py::_check_and_expire_gates`'s 2026-08-28 fix there). This service
never had the coupling bug to begin with: `auth/admin_auth.py` is fully
stateless (JWT, no persisted `admin_authenticated` DB row), applied only as
a `Depends()` guard on the handful of *mutating* routes (`/arm`, `/disarm`,
`/service/enable|disable`, `/autopilot/enable|disable`, `/cycle/run`,
`/kill`, `/ledger/*`, `/reconcile`) — every read route (`/status`,
`/positions`, `/candidates`, etc.) requires no auth. `_trading_loop()` (the
background AUTO cycle) takes no admin/token parameter and never touches
auth at all; it reads `gate.is_armed` / `service_enabled` /
`auto_pilot_enabled` straight from the DB every tick. grep-confirmed the
*only* three places `is_armed` is ever assigned are the three explicit
admin actions above — none reachable from a session simply expiring. Added
an explicit docstring on `_trading_loop()` recording this verification (with
the grep evidence) so a future audit doesn't have to re-derive it. No code
change needed — behavior was already correct.

**Further audit (requested "audit more"):** full re-read of
`capital/ledger.py`, `orders/eod_squareoff.py`, `orders/reconcile.py`,
`resilience/circuit_breaker.py` — all confirmed correct and correctly wired
(`circuit_breaker.record_failure()`/`record_success()` call sites in
`_trading_loop` checked; ledger's daily-loss trip mirroring, lazy reset, and
`reserve_additional()` shortfall-cover logic all consistent).

Cross-checked `orders/eod_squareoff.py`'s once-per-day guard (fires even if
one or more MARKET SELLs fail, no auto-retry that day) against
real-trade-service's `execution/auto_pilot.py::_eod_squareoff` — **same
design, confirmed intentional, not a divergence**: both mark the day's
sweep "done" unconditionally and rely on an operator noticing a failure,
rather than auto-retrying every cycle. real-trade-service surfaces failures
via `notify_async` (Telegram); this service has **no notification channel
at all**.

**New gap found and fixed:** because of the above, a failed EOD flatten in
this service had zero operator-facing signal beyond a server log line and a
per-position `error_message` nobody is prompted to read. Added
`eod_squareoff_stragglers` to `GET /status` (computed: today's sweep already
fired AND ≥1 position still `OPEN`) and a red banner at the very top of the
Overview tab — `⚠ N position(s) still OPEN after today's 3:00 PM EOD
square-off...`. No schema change, no retry-behavior change (kept consistent
with the tested real-trade-service pattern) — pure read-time visibility.

**Verification:** `python3 -m py_compile` clean on every touched/reviewed
file. Isolated `tsc --noEmit` on the two touched frontend files: zero new
errors beyond the same pre-existing, already-documented false positives.

## Session 19 — Run Cycle stage/timing breakdown + full re-audit; 1 new gap fixed

**Feature (requested):** `_run_cycle()` in `main.py` now records a
stage-by-stage breakdown — `reconcile_exits`, `eod_squareoff`, `gate_checks`,
`scan`, `quality_gate`, `entry_attempt` — each with its own `duration_ms`,
plus `total_duration_ms` for the whole cycle. The `scan` stage returns the
top 10 ranked candidates (symbol/window/%change/LTP/score); the
`quality_gate` stage returns every checked candidate's pass/fail + reason +
fundamental/technical/market-cap scores; `entry_attempt` names the exact
symbol attempted and whether it entered. Returned as-is from the existing
`POST /cycle/run` — no new endpoint, no behavior change to trading logic
(instrumentation only, verified with `py_compile`). Frontend: replaced the
old one-line "Last manual cycle: N candidates..." text on the Overview tab
with a new `RunCycleResultPanel` component showing the full stage list,
per-stage timing, and stock names — same visual language as the existing
pipeline dashboard cards. `ScalpCycleResult` types in `positionStocksApi.ts`
updated to match (`ScalpCycleStage`, `ScalpCycleStageCandidate`,
`ScalpCycleStageChecked`).

**Audit (requested — "audit position stock tab fully"):** re-read
`execution/dhan_client.py`, `capital/shared_order_budget.py`, `config.py`,
`tz_utils.py`, and `models.py` end-to-end (all confirmed clean, no changes)
plus main.py's `/status` route against every frontend consumer.

**New gap found and fixed:** `GET /status` has always returned
`shared_order_budget` (`capital/shared_order_budget.py`'s cross-service Dhan
order-rate counter — shared with Real Automatic Trade, same Dhan account)
but no frontend type or display ever consumed it — the shared account-wide
order cap was completely invisible on this dashboard, same "built on the
backend, never wired to the tab" pattern session 12 (candidate log) and
session 18 (reset-daily button) each found once already. Added the field to
`ScalpStatus` and a "Shared Dhan Order Budget" tile to the System Health
grid (Overview tab), amber-highlighted once remaining budget drops under
10%.

**Re-confirmed correct, no changes:** `execution/dhan_client.py` (tick
rounding, security-id cache/collision handling, super-order tag fallback),
`capital/shared_order_budget.py` (fail-open behavior, `check_and_reserve` vs
`record_order_unconditional` call sites in `orders/entry.py` and
`orders/eod_squareoff.py`), `config.py` (env parsing, isolation from
real-trade-service), `tz_utils.py` (holiday list still in sync with
real-trade-service's copy — both list `2026-09-14` as Ganesh Chaturthi),
`models.py` (every column referenced elsewhere in the service is actually
declared).

**Verification:** `python3 -m py_compile main.py` clean. Isolated `tsc
--noEmit` on `PositionStocksTab.tsx` + `positionStocksApi.ts`: zero new
errors beyond the same three pre-existing, already-documented false
positives (missing `node_modules`/`react` types, one `key`-prop artifact) —
no real `npm install`/`npm run build` this session (sandbox network
disabled).

## Session 18 — full re-audit; documented undocumented fixes; 1 new gap fixed

Re-read every backend file end-to-end (main.py, capital/, execution/, feed/,
orders/, screening/, auth/, resilience/, oracle_compat.py, db.py, tz_utils.py,

models.py, config.py) plus positionStocksApi.ts / PositionStocksTab.tsx again,
independent of session 16/17's own pass. `py_compile` and `pyflakes` clean
across the whole service; re-ran the `_COLUMN_MIGRATIONS` vs. `models.py`
consistency check (AST-based) against all 16 entries — clean.

**Found: several real fixes already present in the code from a prior session
were never written up here or in TRACKING.md.** Documenting them now so the
history is accurate (all verified correct by reading the code, not just
trusting the inline comments):
- **`orders/eod_squareoff.py` — a real safety bug, now fixed.** The EOD
  flatten-all sweep's closing SELL used to pass `is_armed=gate.is_armed`
  through to `dhan_client.place_order()`, which raises `DhanNotArmedError`
  whenever not armed, with no exemption for SELL/exit orders (unlike every
  other exit path in this service and in real-trade-service, which
  correctly treat exits as ungated). A disarmed service with open positions
  — e.g. after `/disarm` or `/kill`, or simply never re-armed that morning —
  would hit the EOD sweep, silently fail every closing SELL
  (`DhanNotArmedError`, caught and logged), and leave real-money positions
  **unflattened past 3pm**. Now forced to `is_armed=True` for this call
  specifically, matching `cancel_order`/`cancel_super_order` already being
  unconditionally allowed above it in the same function.
- **Timestamp fix across `/status`, `/positions`, `/trades/history`,
  `/candidates/log`, and `/ledger`.** Every DateTime field is now correctly
  passed through `tz_utils.iso_utc()` before going into a JSON response.
  Previously several of these returned a raw, offset-naive datetime, which
  the browser then parsed as local time instead of UTC — every timestamp on
  the dashboard (armed_at, last_cycle_run_at, opened_at, closed_at,
  created_at, last_synced_from_broker_at) rendered off by exactly +5:30
  (IST). Verified `iso_utc()` is now used consistently at every one of
  these call sites — no stray raw `.isoformat()` calls remain on a
  DB-sourced datetime anywhere in main.py or capital/ledger.py.
- **`orders/entry.py` / `capital/ledger.py` — quantity-floor capital
  shortfall fix (`reserve_additional`)** — confirmed correctly implemented
  and wired: when `max(1, int(position_value / current_ltp))` forces a real
  order to cost more than the risk-sized amount already reserved, the real
  shortfall is now topped up (or the entry is skipped) before the order is
  placed, instead of silently letting `available_capital` drift from reality.
- **`main.py`'s `/dhan/live-orders` — tag-leak fix** — confirmed correct:
  missing-tag orders now default to EXCLUDED (not folded into "SCALP"),
  with a fallback to show everything unfiltered only if literally no order
  in the response carries a `tag` key at all.
- **`capital/ledger.py`'s `release_capital()`** — confirmed the real
  trading-loss kill-switch trip now correctly mirrors onto
  `ScalpGateState` too, not just the ledger's own copy (closes the
  dashboard-visibility gap where `/status` could read "not tripped" while
  `/ledger` correctly showed the trip).

**New gap found and fixed this session:** `POST /ledger/reset-daily` has
existed on the backend for a while (admin-gated, calls `ledger.reset_daily()`
— an explicit manual/emergency reset of today's P&L + kill switch, on top of
the automatic lazy reset-on-date-change) but had **zero frontend wiring** —
no client method in `positionStocksApi.ts`, no button anywhere in
`PositionStocksTab.tsx`. It was only reachable via a raw HTTP call. Fixed:
added `positionStocksApi.resetLedgerDaily()` and a confirm-guarded "Reset
Daily Ledger" button next to Kill Switch in the Overview tab's action row
(same two-step confirm pattern, since it clears real trading state on
demand).

**Also re-confirmed, no changes needed:** `screening/engine.py`'s liquidity
gate (session 12 fix still correct — `continue`, not `pass`), `orders/
adaptive.py`, `resilience/circuit_breaker.py`'s state machine and its two
call sites in main.py, `feed/ws_client.py`'s WS status fields and idle-
timeout handling (session 13/14 fixes still correct), `feed/scrip_master.py`,
`screening/quality_gate.py`, `auth/admin_auth.py`, `auth/dhan_credentials_ro.py`,
`db.py`'s Oracle autoincrement backfill, `oracle_compat.py`. Still open,
not safety-relevant, left for you to decide (unchanged from session 16):
`config.py`'s `MIN_PREFERRED_SCALP_POSITIONS` (declared, never wired into
any gating logic) and `feed/angelone_session.py`'s `rest_headers()` (dead
code, no caller).

**Verification:** `py_compile` + `pyflakes` clean repo-wide (this service).
Real `npm install` (177 packages) + `npm run build` (`tsc && vite build`)
against the real `@types/react`/Tailwind config — zero TypeScript errors,
build succeeded (`dist/assets/index-*.js`, 1,025.85 kB / 273.93 kB gzip —
the pre-existing single-chunk-size warning, unrelated to this session).

---

## Session 17 (this session, continued) — deeper logic audit, 4 more real bugs

Went past infra/wiring into the actual trading-logic math and cross-module
state consistency. Found and fixed four more real bugs (none of them
overlapping with Session 16's event-loop fixes above):

1. **`orders/entry.py` — quantity floor could spend more real money than
   the ledger ever reserved.** `quantity = max(1, int(position_value /
   current_ltp))` floors to at least 1 share, but when the risk-sized
   `position_value` is smaller than one share's price (small pool and/or a
   high-priced stock), that 1 share costs MORE than what
   `ledger.reserve_capital()` already deducted from `available_capital`.
   E.g. ₹3,333 reserved, but a ₹4,500 stock still needs qty=1 — the real
   Dhan order commits ₹1,167 more real rupees than the ledger ever
   accounted for, silently overstating `available_capital` afterwards, and
   since this pool is a software-enforced half of one real, shared Dhan
   account, that unaccounted overspend could eat into real-trade-service's
   half with neither ledger reflecting it. Fixed with a new
   `ledger.reserve_additional()` that tops up the real shortfall before
   the order goes out (or skips the entry with `INSUFFICIENT_CAPITAL_FOR_MIN_QTY`
   if even that isn't available).

2. **`orders/reconcile.py` — a missing fill price could record a phantom
   100%-loss exit.** `_extract_leg_price()`'s last resort, when every price
   field Dhan might return came back empty, was a hard `0.0` — and the
   caller used that directly as `exit_price` with no sanity check:
   `realized_pnl = (0 - entry_price) * quantity` records a fake total-loss
   exit as real, permanent trade history, wrongly starves `release_capital()`
   of the capital that should've come back, and could trip the daily-loss
   kill switch over a data-shape gap that has nothing to do with an actual
   loss. Fixed: falls back to the position's own known target/stop trigger
   price (never null, and realistic — a Super Order leg fills at/near its
   own trigger by construction) instead of 0.0.

3. **`main.py`'s `/dhan/live-orders` — could leak real-trade-service's own
   real orders into this service's view.** real-trade-service places its
   own orders with NO tag at all (`dhan_client.place_order`'s `tag` param
   defaults to `None`, and `entry_engine/entry.py` never passes one). The
   old filter (`o.get("tag", "SCALP")` — defaulting a MISSING key to
   `"SCALP"`) would wrongly fold an untagged real-trade-service order into
   this service's "SCALP" view if Dhan's response omits the `tag` key
   entirely for untagged orders (plausible, unverified without a live
   payload) — the exact cross-service leak the docstring says can't
   happen. Fixed to default missing-tag orders to EXCLUDED, with a
   fallback to show everything unfiltered (with a log line) only if NOT
   ONE order in the whole response carries a `tag` key at all (meaning
   Dhan doesn't distinguish the two services here regardless).

4. **`capital/ledger.py` — the real daily-loss trip never reached the
   gate's copy of the kill switch.** models.py's own docstring already
   flags `ScalpCapitalLedger.daily_loss_kill_switch_tripped` and
   `ScalpGateState.daily_loss_kill_switch_tripped` as "two entirely
   disconnected copies of the same concept," and session13 fixed them
   drifting apart on RESET — but nothing fixed them drifting apart on
   TRIP. `release_capital()`'s real trading-loss trip (the actual daily-
   loss-cap path, as opposed to the manual `/kill` route which sets the
   gate's copy directly) only ever set the ledger's own copy. Since
   `GET /status` — the dashboard's Status/Risk card — reads ONLY the
   gate's copy, an operator would see "Daily Loss Kill Switch: not
   tripped" on the main status card even while the ledger had already
   correctly stopped every new entry at the capital-reservation step
   (entries were never actually at risk either way —
   `reserve_capital()` checks the ledger's own copy directly — this closes
   an operator-visibility gap, not a trading-safety one). Fixed:
   `release_capital()` now mirrors the trip onto `ScalpGateState` too.

## Session 16 (this session) — full re-audit, mapping/wiring check

Read every file in this service again end-to-end (main.py's routing/wiring,
capital/, execution/, feed/, orders/, screening/, auth/, resilience/,
oracle_compat.py, db.py, tz_utils.py, models.py) plus positionStocksApi.ts /
PositionStocksTab.tsx for frontend↔backend field/route mismatches. No
mismatches found this pass — every route in main.py has a matching client
call with matching response field names, and every response field the
frontend reads exists in the backend's return dict.

**One real, previously-missed bug found and fixed** — the same class of bug
already fixed twice earlier this session (`feed/ws_client.py`'s
`get_all_nse_eq()` call, `feed/angelone_session.py`'s `_login()` IP lookup),
but in the three call sites that actually matter most:

`main.py`'s `_run_cycle()` is `async def` and runs on this service's single
event loop, but called three plain-`sync` functions **directly** (no
`asyncio.to_thread`), each of which makes real, blocking network calls:
- `reconcile.run_exit_reconciliation(db)` → `dhan_client.get_super_order_list()`
  (blocking Dhan API call) — ran every 10s tick, unconditionally.
- `eod_squareoff.run_eod_squareoff(db)` → per-position blocking
  `cancel_super_order`/`place_order` Dhan calls — fires once, but with
  several open positions at exactly 3pm.
- `attempt_entry(db, candidate, quality=quality)` → `dhan_client.place_super_order`/
  `place_order` (the real-money order placement itself) — the most important
  of the three, since it's the actual entry path.

Each one blocked the ENTIRE process (every other route this service was
handling — `/health`, `/status`, `/arm`, `/kill`, etc.) for however long that
Dhan round trip took, every time it ran. Fixed by wrapping all three in
`asyncio.to_thread(...)` in `main.py::_run_cycle`, same fix shape as the two
already applied earlier this session. `screening.engine.scan()` was checked
too and is correctly left un-threaded — it's pure in-memory, no I/O.

Also re-confirmed (no changes needed): `screening/quality_gate.py` already
uses a real `httpx.AsyncClient` correctly; `resilience/circuit_breaker.py`,
`capital/shared_order_budget.py`, `capital/ledger.py`, `auth/admin_auth.py`,
`db.py`'s column-migration list, and `oracle_compat.py`'s DDL-error-swallow
list all match the rest of the codebase's conventions with no drift found.
Confirmed still-open, not-safety-relevant, left for you to decide: `config.py`'s
`MIN_PREFERRED_SCALP_POSITIONS` (declared, never wired into any gating logic)
and `feed/angelone_session.py`'s `rest_headers()` (dead code, no caller).

## Files modified this session (session 12)

```
services/position-stocks-service/
├── screening/engine.py              ← FIXED: liquidity gate `pass` → `continue`
├── main.py                          ← NEW: GET /candidates/log
├── STATUS.md                        ← this file, updated
└── TRACKING.md                      ← updated with session 12 entry

frontend/src/
├── positionStocksApi.ts             ← NEW: ScalpCandidateLogRow type + candidatesLog()
└── components/PositionStocksTab.tsx ← restructured into 6 sub-tabs + new Candidate Log table
```

## Completed steps (tracking doc §5)

| Step | Item | Status | Notes |
|------|------|--------|-------|
| 1 | Folder structure + docker-compose entry | ✅ DONE | `services/position-stocks-service/`, port 8006, compose entry added |
| 2 | Angel One true-WS client | ✅ DONE | `feed/ws_client.py` — binary frame parser, subscribe/heartbeat/reconnect; `feed/angelone_session.py` (duplicate of market-data-service session with feed_token surfaced); `feed/scrip_master.py` (duplicate of angelone_scrip_master.py) |
| 3 | Dhan symbol→security_id side-map | ✅ DONE | `execution/dhan_client.py` — full duplicate of real-trade-service's dhan_client.py with Super Order methods added; `auth/dhan_credentials_ro.py` — read-only credential reader |
| 4 | Rolling-window screening engine + composite ranking | ✅ DONE | `screening/engine.py` — in-memory ring buffer, 5m/15m/60m rolling pct-change, composite score, candidate dataclass |
| 5 | Super Order integration | ✅ DONE | `orders/entry.py` — gates → adaptive levels → capital sizing → place_super_order; `orders/adaptive.py` — adaptive target/stop formula; first-live-order qty=1 override wired in |
| 6 | ScalpCapitalLedger + 50/50 enforcement | ✅ DONE | `capital/ledger.py` — reserve/release capital, daily-loss kill switch, sync from Dhan fund balance |
| 7 | 3:00 PM square-off sweep + order budget guard | ✅ DONE | `orders/eod_squareoff.py`; order budget counter in ScalpGateState; Redis guard deferred (see Open Items) |
| — | Super Order exit reconciliation monitor | ✅ DONE (this session) | `orders/reconcile.py` — polls `dhan_client.get_super_order_list()` every trading-loop tick (runs even while disarmed), detects TARGET_LEG/STOP_LOSS_LEG fills + dead ENTRY_LEG rejections, closes the `ScalpPosition` row, calls `ledger.release_capital()`. Wired into `main.py`'s `_trading_loop()` (runs first, before the armed-gate check) and exposed as `POST /reconcile` for an on-demand pass. **Fill-price extraction on the exit legs is a best-effort guess** (Dhan's public sample payload doesn't show `averageTradedPrice` on nested `legDetails` entries) — flagged loudly in the module docstring; eyeball the first few real TARGET_HIT/STOP_HIT rows against Dhan's own app. |
| 8 | Frontend "Position Stocks" tab | ✅ DONE (this session) | `frontend/src/positionStocksApi.ts` (own client/own localStorage URL key, mirrors `realTradeApi.ts`'s isolation pattern) + `frontend/src/components/PositionStocksTab.tsx` (armed/market/WS/kill-switch strip, arm/disarm/kill/sync/reconcile buttons, capital ledger card, 5m/15m/60m screener grouped view, open + closed-today position lists). Wired into `App.tsx`: new `"positionstocks"` tab, nav item (📈 Position Stocks), command-palette entry. |
| 9 | RISK_PER_TRADE_PCT from user | ✅ DONE | **User confirmed 2%.** `config.py`: `RISK_PER_TRADE_PCT=2.0`, `RISK_PER_TRADE_PCT_CONFIRMED=True`. Startup warning and frontend banner no longer fire. |
| 10 | Master module enable/disable toggle | ✅ DONE (session 4) | `models.py`: `ScalpGateState.service_enabled` (default True), independent of `is_armed`. `main.py`: `POST /service/enable`, `POST /service/disable`, `service_enabled` added to `/status`. Frontend: "Module" status tile + Enable/Pause buttons in `PositionStocksTab.tsx`. Reconciliation + EOD squareoff ignore this flag by design (§3.7 "no exceptions"). |
| 11 | 1-minute screening window | ✅ DONE (session 4) | `config.py`: `SCAN_WINDOWS_MINUTES = [1, 5, 15, 60]`, `MIN_PCT_CHANGE_1M` (default 0.5%). `screening/engine.py`: `_WINDOW_THRESHOLDS` now has a 4th entry, feeds the same composite-ranking step. Frontend: window filter + grouped screener view both include "1m" (grid widened to 4 cols). |
| 12 | Auto-Pilot toggle + manual Run Cycle | ✅ DONE (session 6) | `models.py`: `auto_pilot_enabled` (independent of `is_armed`/`service_enabled`). `main.py`: shared `_run_cycle(db, trigger)` used by both the 10s loop and `POST /cycle/run`; `POST /autopilot/enable`/`disable`; `asyncio.Lock` prevents overlap. Frontend: Auto-Pilot tile + buttons, Run Cycle Now button + result banner. |
| 13 | Quality gate (fund/tech/news/bulk-deal) | ✅ DONE (session 6) | New `screening/quality_gate.py` — best-effort, ≤2.5s timeout, fail-open, applied only to top `QUALITY_GATE_TOP_N` (default 3) candidates. Reuses analysis-intelligence-service's existing fundamental/technical/event endpoints (same ones real-trade-service's own quality gate uses). Logged to `ScalpCandidateLog`'s new columns for full audit. |
| 14 | Live Dhan order tracking | ✅ DONE (session 6) | New `GET /dhan/live-orders` — calls existing `dhan_client.get_super_order_list()`, filters to `tag="SCALP"`. Frontend: Live Dhan Order Activity panel, own 30s poll + manual refresh. |
| 15 | Trade history dashboard (buy/sell/P&L) | ✅ DONE (session 6) | New `GET /trades/history` — summary (win rate, total P&L, best/worst) + full trade list with `exit_price` (previously missing from `/positions` entirely despite the column existing since session 1 — now added there too). Frontend: Trade History section with summary cards + table. |
| 16 | Circuit breaker reset bug | ✅ FIXED (session 7) | `resilience/circuit_breaker.py`'s `is_open()` was resetting a locally-shadowed `_failure_count` instead of the module-level one (missing from its `global` declaration) — the failure count never actually cleared after the 60s reset window, so one failure right after reset would immediately re-trip the breaker. Fixed. |
| 17 | Dead code cleanup (both services) | ✅ DONE (session 7) | `pyflakes`-driven pass found unused imports/variables in `main.py`, `screening/engine.py`, `execution/dhan_client.py`, `feed/angelone_session.py`, `feed/scrip_master.py`, `feed/ws_client.py`. Both services now pass `pyflakes` with zero findings repo-wide. |
| 18 | Shared Dhan order-rate guard — actually built | ✅ DONE (session 7) | §3.8 had been described as complete for several sessions with **zero actual code** anywhere in the repo (confirmed via repo-wide search). Built for real on both services this session — see TRACKING.md §3.14 for full detail. New `SharedOrderBudget` model (both services' `models.py`), `capital/shared_order_budget.py` (position-stocks) / `execution/shared_order_budget.py` (real-trade), wired into `orders/entry.py` (gated) + `orders/eod_squareoff.py` (unconditional) on this service, and `manual_engine.py` (gated, manual BUY only) + `exit_engine.py`'s `_send_real_sell` (unconditional) on real-trade-service. |
| 19 | `daily_loss_kill_switch_tripped` model/migration mismatch | ✅ FIXED (session 11) | See TRACKING.md's session-11 entry — migration-only field with no matching ORM `Column`, so `/ledger` 500'd no matter how many times the DB got migrated. Fixed in `models.py`. |
| 20 | Frontend sub-tabs | ✅ DONE (session 12) | `PositionStocksTab.tsx` was one continuous scroll; now 6 sub-tabs (Overview/Screener/Positions/Trade History/Dhan Live Orders/Settings). Admin auth + critical banners stay above the tabs. Pure reorg, no behavior change. |
| 21 | Liquidity-gate bug (screening engine) | ✅ FIXED (session 12) | `screening/engine.py`'s min-avg-volume gate (§3.3) computed the check but the branch was a bare `pass` — never actually skipped a low-activity symbol. Fixed to `continue`. Same class of bug as #18 above (documented gate, no real enforcement). |
| 22 | `GET /candidates/log` | ✅ DONE (session 12) | Flagged since session 6 as a cheap, ready-to-build follow-up (pure DB read of `ScalpCandidateLog`, no external calls) — see "Next steps" #9 below, now resolved. Frontend: new "Candidate Log" table on the Screener sub-tab. |

### Deploy fixes applied (carried over from session 3, confirmed intact in this zip)
- `requirements.txt`: `python-oracledb` → `oracledb==2.5.1` (invalid package name fixed).
- `docker-compose.yml`: added the missing `${ORACLE_WALLET_HOST_DIR:-./oracle_wallet}:/oracle_wallet:ro`
  volume mount plus explicit `ORACLE_WALLET_DIR`/`TNS_ADMIN` env vars (was causing `DPY-4026` on boot).
- `RISK_PER_TRADE_PCT=2.0` / `RISK_PER_TRADE_PCT_CONFIRMED=true` set directly in the compose block.

### Bug fixed this session (found while wiring the toggle in)
- `main.py`'s trading loop previously checked `gate.is_armed` *before* the EOD squareoff
  check, so a disarmed service with open positions would silently skip the hard 3:00 PM
  flat sweep — exactly the case tracking doc §3.7 ("no exceptions") exists to prevent.
  Reordered: reconciliation → EOD squareoff check (both now fully unconditional) →
  `service_enabled` gate → `is_armed` gate → screening/entry.



## Files created this session

```
services/position-stocks-service/
├── TRACKING.md                      ← continuity anchor (copy of tracking doc)
├── STATUS.md                        ← this file
├── config.py
├── db.py
├── models.py
├── oracle_compat.py                 ← copied from real-trade-service (identical)
├── tz_utils.py                      ← copied from real-trade-service (identical)
├── main.py                          ← FastAPI app + trading loop
├── requirements.txt
├── Dockerfile
├── auth/
│   ├── __init__.py
│   └── dhan_credentials_ro.py
├── capital/
│   ├── __init__.py
│   └── ledger.py
├── execution/
│   ├── __init__.py
│   └── dhan_client.py
├── feed/
│   ├── __init__.py
│   ├── angelone_session.py
│   ├── scrip_master.py
│   └── ws_client.py
├── orders/
│   ├── __init__.py
│   ├── adaptive.py
│   ├── entry.py
│   └── eod_squareoff.py
├── resilience/
│   ├── __init__.py
│   └── circuit_breaker.py
└── screening/
    ├── __init__.py
    └── engine.py
```

Also modified: `docker-compose.yml` — `position-stocks-service` block added at the end.

### Files created/modified THIS session (exit reconciliation + frontend tab)

```
services/position-stocks-service/
├── main.py                          ← MODIFIED: imports orders.reconcile, runs it every
│                                        trading-loop tick (unconditionally, before the
│                                        armed-gate check), added POST /reconcile route
└── orders/
    └── reconcile.py                 ← NEW: exit reconciliation monitor

frontend/src/
├── positionStocksApi.ts             ← NEW: own API client (own localStorage URL key,
│                                        never shares a client with api.ts/realTradeApi.ts)
├── components/
│   └── PositionStocksTab.tsx        ← NEW: the dashboard tab itself
└── App.tsx                          ← MODIFIED: import, "positionstocks" Tab type,
                                         navItems entry (📈 Position Stocks), command-palette
                                         entry, render branch
```

Everything from the previous session (folder structure, WS client, Dhan side-map,
screening engine, Super Order integration, capital ledger, EOD sweep) is unchanged —
see the table above for those.

---

## 🔴 First deploy attempt — broken, root cause found (session 5, 2026-09-12)

User deployed this service for the first time and reported: every button on the
Position Stocks tab looks disabled/non-functional, a red banner shows nginx's own
`405 Not Allowed` error page verbatim, and the top strip shows `DISARMED` /
`CLOSED` / `DOWN` / `PAUSED` / a "RISK_PER_TRADE_PCT not confirmed" warning —
all at once, on first load, before touching any button.

**Root cause: not a code bug in this service — a missing reverse-proxy route.**
`deploy/nginx-stockky.conf` (the VM-level nginx in front of everything) only ever
had three location blocks: `/` (frontend), `/api/` (api-gateway :8000), and
`/realtrade/` (real-trade-service :8005). **It never got a `/positionstocks/`
block routing to port 8006** when this service was built in earlier sessions —
that step was missed.

What that caused, concretely: whatever URL was pasted into the Position Stocks
Settings box, if it pointed at the same nginx-fronted domain, fell through to the
`/` block — the frontend container's own static-file server. Nginx's default
static handler only serves `GET`/`HEAD`; every `POST` (`/arm`, `/disarm`,
`/service/enable`, `/service/disable`, `/kill`, `/ledger/sync`, `/reconcile`) hit
that handler and got its stock `405 Not Allowed` page back verbatim — which is
exactly the banner in the screenshot. The `GET` calls (`/status`, `/positions`,
`/candidates`, `/ledger`) would instead have fallen through to the SPA's
`index.html` (200 OK, but HTML instead of JSON) — so `/status` never actually
returned real data, which is why `armed`, `service_enabled`, and `risk_confirmed`
all read as `false`/undefined in the frontend (`status` was effectively `null`)
and every tile/banner derived from it looked "off" simultaneously. This was one
root cause showing up in five places, not five separate bugs.

**Fixed this session:** added the missing `stockky_position_stocks` upstream
(`127.0.0.1:8006`) and a `location /positionstocks/` block to
`deploy/nginx-stockky.conf`, mirroring the existing `/realtrade/` pattern exactly
(same trailing-slash strip behavior, same header/timeout settings, own trust
boundary).

**Action needed on your VM (not something I can do from here):**
1. Replace `/etc/nginx/sites-available/stockky` with the updated
   `deploy/nginx-stockky.conf` from this zip.
2. `sudo nginx -t && sudo systemctl reload nginx`
3. In the Position Stocks tab's Settings, set the service URL to
   `https://stockky.duckdns.org/positionstocks` (no trailing slash — the app
   appends paths like `/status` itself, and nginx's trailing-slash `proxy_pass`
   strips the `/positionstocks` prefix before forwarding, same as `/realtrade/`).
4. Reload the tab. All tiles/buttons should reflect real backend state once
   `/status` actually returns JSON instead of nginx's fallback.

If after this fix anything is *still* wrong, that's the point to suspect an actual
code bug (e.g. a CORS issue, or the container itself not booting) rather than the
routing gap — check the container logs directly (`docker compose logs
position-stocks-service`) as the next diagnostic step.



1. ~~RISK_PER_TRADE_PCT~~ — **RESOLVED session 3: user confirmed 2%.**

2. ~~Shared order counter~~ — **RESOLVED session 3.** Built as a DB-backed counter
   instead of Redis (deliberate deviation, reasoned in `capital/shared_order_budget.py`'s
   docstring: this codebase's Redis layer is optional/off-by-default, so a real-money
   rate guard was built against the one piece of infra both services unconditionally
   share — the same physical DB). Table `stockky_shared_order_budget`. Wired into
   `orders/entry.py` (gated), `orders/eod_squareoff.py` (unconditional record, never
   blocks a forced exit), `/status` endpoint, and real-trade-service's manual-order path.

3. **Exit-leg fill price is a best-effort guess** (see `orders/reconcile.py` docstring) —
   Dhan's public API sample doesn't document an `averageTradedPrice` field on the nested
   `legDetails` dicts for TARGET_LEG/STOP_LOSS_LEG, only on the top-level entry leg. The
   monitor tries several plausible field names and falls back to the leg's static
   trigger price, which is close but not guaranteed exact. **Recommended: after the
   first few real exits fire, compare the booked `realized_pnl` in `/positions` against
   Dhan's own order history for those same trades** — if they match, this is a non-issue;
   if not, the field name needs adjusting (quick fix, one function).

4. **Frontend: type-checked, not build-tested.** Session 3: sandbox has no real npm
   registry egress (metadata resolves, tarball downloads 403), so a full
   `npm install && npm run build` still hasn't run. Instead ran the real TypeScript
   parser/compiler against the new files: `positionStocksApi.ts` and
   `PositionStocksTab.tsx` passed a strict, isolated `tsc` type-check against
   stubbed React types with **zero real errors** (two stub-artifact false positives
   confirmed by matching identical already-shipped patterns in `RealAutoTrade.tsx`:
   inline `onChange={e => ...}` handlers and `key={...}` on custom components). Full
   `App.tsx` (2100 lines) passed a real syntax-only parse (`ts.createSourceFile`,
   zero errors) and its four new-tab wiring points (import, `Tab` union member, two
   nav-array entries, ternary render branch) were manually diffed against the
   existing `realtrade` tab's identical shape — all consistent. **Still recommend
   running `npm install && npm run build` once on your VM before deploying** to catch
   anything type-resolution against the real `@types/react`/Tailwind config would
   catch that a stub can't (e.g. genuine prop-type mismatches against real React's
   generics) — but structurally and syntactically this is now much more confidently
   clean than a static read-through alone.

---

## Next steps (in priority order, current as of session 18)

Everything below requires your live VM / market hours and can't be verified
from this sandbox. All code-level work identified through session 18's audit
is done — this list is now purely about live verification, not open bugs.

1. **Redeploy this zip and confirm clean boot.** Watch for `init_tables()`
   completing (including the `_ensure_columns()` migration log lines, which
   should be no-ops by now on an already-migrated DB) and the WS client
   connecting. Confirm the Position Stocks tab shows real `armed`/`market`/
   `ws`/`module` state.
2. **Re-verify dashboard timestamps read correctly** (session 18's `iso_utc`
   documentation fix) — armed_at, last_cycle_run_at, position opened_at/
   closed_at, candidate log created_at, ledger last_synced_from_broker_at
   should all show in your local timezone correctly, not off by +5:30.
3. **Exercise the new "Reset Daily Ledger" button once** (session 18) in a
   safe moment (e.g. right after a deploy, before arming) to confirm it
   round-trips correctly — should clear `realized_pnl_today` and the kill
   switch without touching `available_capital`.
4. **First live Super Order** — with `FIRST_LIVE_ORDER_MIN_QTY_OVERRIDE=true`
   (default), arm the service during market hours and watch the very first
   real entry fire at qty=1. Confirm the fill shape matches expectations,
   then flip the override off.
5. **Confirm the EOD squareoff fix actually flattens positions when
   disarmed** (session 18's most safety-relevant fix) — if you get the
   chance, deliberately leave the service disarmed with an open position
   near 3pm once (small size) and confirm the SELL still fires instead of
   silently failing. Not urgent to force, but worth keeping in mind the old
   behavior would have failed silently here.
6. Watch the first few real TARGET_HIT/STOP_HIT exits and cross-check
   `realized_pnl` against Dhan's own order history (exit-leg fill price is
   still a best-effort field-name guess — see `orders/reconcile.py`'s
   docstring).
7. Once live, watch how often the 1m window actually fires vs. 5m/15m/60m —
   tune `MIN_PCT_CHANGE_1M` up if it's mostly noise.
8. **Set the analysis-intelligence-service URLs for the quality gate** —
   `ANALYSIS_INTELLIGENCE_URL` (or the individual `FUNDAMENTAL_URL`/
   `TECHNICAL_URL`/`EVENT_URL` overrides) in the service's env, if not
   already set. Without these, `screening/quality_gate.py` fails open
   silently (safe, but the fund/tech/news signal isn't doing anything) —
   check logs for "quality_gate: ... fetch failed" to confirm it's reaching
   the service.
9. **Verify the shared Dhan order-rate guard** — check `/status`'s
   `shared_order_budget` field after a few real orders, and watch
   real-trade-service's logs for "SHARED Dhan order budget exhausted" or a
   "failing open" line to confirm it's actually reachable and counting.
10. **Out of scope, worth knowing:** real-trade-service still has a
    pre-existing `pyflakes` backlog in files no session has touched
    (`notifier.py`, `auth/admin_auth.py`, `offline_test_harness.py`,
    `adaptive_thresholds.py`, `market_feed/feed.py`,
    `watchlist_engine/dynamic_universe.py`, `db.py`,
    `execution/auto_pilot.py`, `execution/reconcile.py`,
    `execution/dhan_client.py`, a couple of `scripts/`) — left alone
    deliberately since it trades real money live and deserves its own
    dedicated session.

---

## How to resume

Attach `TRACKING.md` + the latest repo zip in a new conversation.
Everything needed to continue is captured here — no re-derivation needed. Just say
which of the "Next steps" above to continue from, or describe what's changed.

Each time you come back to this project, expect this loop: read this file +
TRACKING.md first, report back what's done vs. what's next in plain terms, then work
through the next step(s) and hand back an updated zip in the same structure —
repeating step by step rather than dumping everything at once.
