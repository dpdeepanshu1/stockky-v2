# Position Stocks — Project Tracking Document
**Purpose of this doc:** continuity anchor. If chat context/limits reset, attach this doc + the latest Stockky zip in a new conversation and work continues from exactly here — nothing re-derived from scratch.

**Last updated:** 2026-09-13 (session 14)
**Status:** Session 12 did what the user asked for directly: (1) added sub-tabs
to the Position Stocks frontend page (it was one long scroll of every section
at once — now Overview / Screener / Positions / Trade History / Dhan Live
Orders / Settings), (2) a full audit pass ("check every wiring/mapping, any
open or empty function") that found one real bug — `screening/engine.py`'s
liquidity gate computed a real threshold check but the branch below it was a
bare `pass`, so it never actually skipped a low-activity symbol; the gate
documented in §3.3 was doing nothing. Fixed (see §3.15). Also built the
`GET /candidates/log` endpoint that STATUS.md had flagged since session 6 as
a cheap, ready-to-build follow-up and surfaced it as a new "Candidate Log"
table on the Screener sub-tab. Everything else from sessions 1-11 (below)
is unchanged. Full detail in §3.15.

**Status (session 7, prior):** Session 7 was a full audit pass across BOTH position-stocks-service
and real-trade-service ("check every wiring and mapping, open or incomplete
code") — found and fixed a real circuit-breaker reset bug, cleaned up dead
code repo-wide (pyflakes now clean on both services), and — the significant
finding — discovered that the §3.8 shared Dhan order-rate guard had been
described as fully built across several earlier sessions' docs with **no
actual code anywhere in the repo**. Built for real this session, on both
services. Full detail in §3.14. Session 6's feature set (Auto-Pilot, quality
gate, live Dhan orders, trade history) and session 5's nginx fix are
otherwise unchanged and still awaiting live verification — that remains the
top blocker before any of this can be exercised for real. All
application-level work (§5, steps 1–14) is otherwise complete: (session 4) the
`service_enabled` migration caveat is now an automatic idempotent DB migration
(`db.py::_ensure_columns()`), and a real `npm install && npm run build` against
the actual npm registry confirmed zero TypeScript errors on the new frontend
code (re-confirmed again this session, session 6). Still not yet smoke-tested
against a live Dhan/Angel One session — that, the first live order, and
1m-window/quality-gate tuning all require the live VM + market hours and can't
be done from a sandbox. User wants to go straight to REAL money live testing
(no DEMO/paper phase) once deployed — see
STATUS.md for the full up-to-date checklist and next steps; this file stays the
static architecture record, STATUS.md is the living progress tracker.

---

## 1. Background — existing system (context, not the new work)

- **Stockky v2**: microservices platform for Indian stock scanning/analysis/auto-trading. Python FastAPI backend, React/TS frontend. Repo: `github.com/dpdeepanshu1/stockky-v2`. Deployed via docker-compose on an Oracle Cloud VM (aarch64/ARM), directory `~/stockky-v2`.
- Services: `api-gateway`, `market-data-service`, `analysis-intelligence-service`, `decision-prediction-service`, `notification-scheduler-service`, `real-trade-service`, `frontend`, and now `position-stocks-service`.
- `real-trade-service` already trades **real money live on Dhan** and has been running for weeks. It uses:
  - `execution/dhan_client.py` — plain `place_order()` / `cancel_order()`, no Super Order usage anywhere.
  - Entries: `order_type="LIMIT"`, `product_type="CNC"` (held for days).
  - Exits: `order_type="MARKET"` (hardcoded in `exit_engine/exit.py`) — this is the proven, reliable path.
  - Price source for decisions: REST-polled `market-data-service` (yfinance + a 3s-interval AngelOne poll dressed up as `angelone_ws_feed.py` — **confirmed by reading the code that this is NOT a real WebSocket**, despite the filename).
  - `market-data-service/angelone_client.py` already holds working AngelOne login (`ANGELONE_CLIENT_ID/MPIN/API_KEY/TOTP_SECRET` env vars) and already retrieves a `feed_token` on login (currently unused — this is exactly what a real WS connection needs).
  - `market-data-service/angelone_scrip_master.py` already provides a free, no-auth symbol→AngelOne-token map for all NSE-EQ symbols.
- Full history of bugs fixed / decisions made in the existing system (28+ decisions) lives in SyncContext project `STOCKKY` and in chat memory — not repeated here since this doc is scoped to the **new module only**.

---

## 2. The new idea — "Position Stocks"

Inspired by Groww's "Price change > 1%" screener (5 min / 15 min / 1 hour windows, screenshotted by user). Goal: a **separate, extremely fast, fully isolated** trading pipeline that:
- Scans for stocks moving fast in 5m/15m/1h windows.
- Enters quickly, targets **3–8% profit**, stoploss **2–5%**, both **adaptive** (not fixed numbers).
- Holds anywhere from minutes to a few hours, always adaptive — but **hard flat by 3:00 PM IST**, no exceptions.
- Runs on its **own 50% of total trading capital**, completely separate from `real-trade-service`'s pool.
- Must never fail due to unrelated system slowness — no shared dependency with the existing trading loop.

---

## 3. Final architecture decisions (all confirmed by user)

### 3.1 Isolation
- Standalone container: `position-stocks-service`. Own process, own event loop, own docker-compose entry.
- No runtime imports from `real-trade-service`. Dhan auth / security-id-cache / order-placement helpers are **duplicated** into this service's own module (copied logic, not shared code) — a bug or slowdown in one can never touch the other.
- Own `/health` endpoint, own circuit breaker, own daily-loss kill switch (tighter threshold than the core system, since scalping is higher variance).

### 3.2 Data feed — Angel One WebSocket (FREE)
- **User rejected Dhan's Data API (₹499/mo, confirmed via user's own screenshot of `web.dhan.co/index/profile` — flat subscription, not trade-count-waived as some third-party sites claimed).**
- Decision: use **Angel One SmartAPI WebSocket (`smartWebSocketV2`)** instead — genuinely free, no subscription.
- Reuses existing `AngelOneSession` login flow (already gets `feed_token`) and existing `angelone_scrip_master.py` symbol→token map.
- Built: a true persistent WS client (binary frame parsing, subscribe/heartbeat/reconnect) replacing the old fake "ws_feed" (REST polling every 3s). `feed/ws_client.py`.
- Data comes from **Angel One** (symbol → AngelOne token), orders execute on **Dhan** (symbol → Dhan `security_id`) — the service keeps both maps side by side, keyed by the same clean symbol string.

### 3.3 Screening engine
- In-memory per-symbol ring buffer: (timestamp, LTP, volume). O(1) rolling %-change computation over trailing 5m/15m/1h per tick — no DB read on the hot path.
- Gates layered on top: min liquidity/avg volume, max bid-ask spread, exclude symbols already in an open scalp position, exclude illiquid/penny stocks.
- Runs **all three windows simultaneously** as independent candidate sources feeding one shared ranking step (composite score) — best candidate(s) win regardless of which window surfaced them.

### 3.4 Order execution — Dhan Super Order (bracket), FREE, chosen over manual OCO
- Super Order is part of Dhan's **Trading API**, unconditionally free (unlike the Data API).
- One call places entry + target + stoploss legs atomically.
- `product_type` = **`"INTRA"`** — NOT `"CNC"` (real-trade-service's multi-day-hold type).
- `price` (entry reference) must be > 0 even when `order_type="MARKET"`; Dhan validates target/SL against it. Plan: pass the current Angel One LTP as this reference at fire time; actual fill still executes at market.
- Recommended (not mandatory) first-live-trade safety valve implemented: `FIRST_LIVE_ORDER_MIN_QTY_OVERRIDE` forces qty=1 on the very first live Super Order.

### 3.5 Capital sizing — adaptive, risk-based — ✅ CONFIRMED
`position_value = (fixed % of the scalp pool risked per trade) ÷ (that stock's own adaptive stoploss %)`

**Confirmed by user this session: 2% of the scalp pool risked per single trade.**
`config.RISK_PER_TRADE_PCT = 2.0`, `config.RISK_PER_TRADE_PCT_CONFIRMED = True`. The
startup warning in `main.py` (which fired on every boot while this was an open item)
is now silent. A volatile stock with a wider adaptive stop automatically gets a
smaller position; a calmer stock gets a bigger one — rupee-at-risk stays roughly
constant per trade.

### 3.6 Capital split
- 50/50 with `real-trade-service`, enforced entirely in software via `ScalpCapitalLedger` — Dhan itself does not segregate a single account's funds into pools. Both services independently check their own ledger's remaining allowance, cross-checked against Dhan's live fund-limit API.

### 3.7 Position limits & hold time
- Max **5** concurrent scalp positions. Min **1** preferred but never forced onto a bad/absent signal.
- No fixed hold timer — adaptive target/SL, exits the instant either triggers (Super Order's own bracket legs).
- Hard **3:00 PM IST** square-off sweep force-closes anything still open, MARKET order, no exceptions.

### 3.8 Shared broker-side constraint — ✅ implemented
- Dhan's account-wide order cap (~5,000–7,000 orders/day) is shared between `real-trade-service` and this service (same Dhan account).
- **Built as a DB-backed counter, not Redis** (deviation from the original plan, flagged and reasoned in `capital/shared_order_budget.py`'s docstring): this codebase's Redis layer is Upstash-based and optional/off-by-default, so a real-money order-rate guard was made to depend on the one piece of infrastructure both services unconditionally share instead — the same physical Postgres/Oracle DB. Table `stockky_shared_order_budget`, identical module duplicated into both services (`capital/shared_order_budget.py` in position-stocks-service, `execution/shared_order_budget.py` in real-trade-service).
- Soft rate governor, not a financial ledger: one read + one upsert per order attempt, fails **open** (allows the order) on any DB error — a broken rate-governor must never itself block a real exit.
- Forced exits (EOD squareoff, manual kill-switch closes) call `record_order_unconditional()` — tracked for visibility, never gated, matching §3.7's "no exceptions" rule.
- Wired into: `orders/entry.py` (gated, checked before capital is reserved), `orders/eod_squareoff.py` (unconditional record), `main.py`'s `/status` endpoint (`shared_order_budget` field), and real-trade-service's manual-BUY path (`manual_engine.py`, gated) and manual/auto-SELL paths (unconditional record, matching that codebase's existing "exits always allowed" convention).

### 3.9 Dashboard
- Frontend tab: **"Position Stocks"** (`positionStocksApi.ts` + `PositionStocksTab.tsx`, wired into `App.tsx`).
- Live screener grouped by window (5m/15m/1h), open scalp positions with live P&L, today's pool P&L, remaining scalp capital, manual kill switch.

### 3.10 Rollout
- **User's explicit choice: go straight to REAL money, live market, no DEMO/paper phase** — based on weeks of hands-on trust with `real-trade-service`. Risk flagged once (new untested order-type path); proceeding per user's decision.

### 3.11 Master module toggle — ✅ implemented (session 4)
- New `ScalpGateState.service_enabled` (default `True`), independent of `is_armed`.
- `is_armed` only gates whether real orders can be **placed**; `service_enabled` gates
  the **whole module** — screening and entries both stop when it's off — so the
  service can be paused entirely (maintenance, ruling it out while debugging
  something unrelated) without losing/re-setting the arm state.
- New endpoints `POST /service/enable` / `POST /service/disable`, surfaced in
  `/status` as `service_enabled`, and in the frontend as a "Module" status tile +
  Enable/Pause buttons on `PositionStocksTab.tsx`.
- Exit reconciliation and the EOD square-off sweep **ignore this flag** (and
  `is_armed`) by design — open real-money positions must never be left unmanaged
  just because the module is toggled off (§3.7's "no exceptions" rule).
- **Ordering fix, same session:** `main.py`'s trading loop previously ran the EOD
  squareoff check *after* the `is_armed` check, meaning a disarmed service with open
  positions would silently skip the hard 3pm flat sweep. Moved EOD squareoff (and
  reconciliation, already unconditional) ahead of both the `service_enabled` and
  `is_armed` gates.
- **Migration note — RESOLVED session 4 (was a manual caveat, now automatic):**
  SQLAlchemy's `create_all()` only creates missing tables, never ALTERs existing ones.
  `db.py`'s `init_tables()` now also runs `_ensure_columns()` — an idempotent,
  inspector-based check that ALTERs any table missing a column added to `models.py`
  since the table was first created. Currently covers
  `scalp_gate_state.service_enabled`; future column additions just need one entry
  added to `db.py`'s `_COLUMN_MIGRATIONS` list, no more manual `ALTER TABLE` steps
  or "check before you deploy" caveats.

### 3.12 1-minute screening window — ✅ implemented (session 4)
- Added as a 4th window alongside 5m/15m/60m: `SCAN_WINDOWS_MINUTES = [1, 5, 15, 60]`,
  `config.MIN_PCT_CHANGE_1M` (default 0.5%, meaningfully lower than 5m's 1.0% since a
  1-minute move needs less %-change to be notable).
- Intentionally the noisiest/fastest-triggering window — most likely to fire on a
  single flickering tick rather than real momentum. Threshold is a starting point;
  tune based on what it actually surfaces once live.
- Feeds the same composite-score ranking step as the other three windows — no
  special-casing in the entry/capital-sizing path.
- Frontend: `PositionStocksTab.tsx`'s window filter and grouped screener view both
  updated to include "1m" (grid widened to 4 columns to fit it).

### 3.13 Auto-Pilot, manual cycle, quality gate, Dhan-live + trade-history dashboard — ✅ implemented (session 6)
User asked for four things in one go: an auto-pilot toggle + manual "run cycle"
button; a dashboard that mirrors real-trade-service's order-tracking/Dhan-live
tabs; a proper buy/sell/P&L view; and quick fundamental/technical/news/bulk-deal
screening so the engine picks better stocks, not just fast-moving ones.

- **Auto-Pilot toggle** (`ScalpGateState.auto_pilot_enabled`, default True) — a
  third, finer switch alongside `is_armed` (real orders) and `service_enabled`
  (whole module). Screening still runs and `/candidates` stays live whenever
  armed+service_enabled+market-open, regardless of this flag; only the
  *automatic* entry attempt each cycle is gated by it. `POST /autopilot/enable`
  / `POST /autopilot/disable`, surfaced in `/status` and as its own dashboard
  tile + buttons.
- **Manual "Run Cycle Now"** — `POST /cycle/run` forces one scan+quality-gate+
  entry pass immediately, bypassing the 10s timer *and* `auto_pilot_enabled`
  (still requires `is_armed` + `service_enabled` — a paused/disarmed service
  won't fire a manual cycle either). Refactored the background loop's body
  into one shared `_run_cycle(db, trigger)` function used by both the 10s
  auto-loop (`trigger="AUTO"`) and this endpoint (`trigger="MANUAL"`) — they
  can never drift apart in behavior since it's the same code. An `asyncio.Lock`
  (`_cycle_lock`) prevents the two from ever overlapping.
- **Quality gate** (`screening/quality_gate.py`, new file) — the price/volume
  screener has no idea *why* a stock is moving. This adds a fast, best-effort
  check for fundamental score, technical score, market cap, and a positive-
  news/bulk-deal signal (`has_positive_catalyst`, from analysis-intelligence-
  service's existing event-depth scoring), applied ONLY to the top
  `config.QUALITY_GATE_TOP_N` (default 3) ranked candidates each cycle — never
  the whole scan universe, so it stays "quick" as asked. Mirrors real-trade-
  service's own candidate_engine quality-gate pattern and reuses the SAME
  shared analysis-intelligence-service endpoints (a shared read-only backend,
  not real-trade-service's own state — doesn't compromise this service's
  isolation from real-trade-service specifically). Every call has a short
  timeout (`config.QUALITY_GATE_TIMEOUT_S`, default 2.5s — far shorter than
  real-trade-service's 12-60s, since this loop ticks every 10s) and fails
  OPEN: missing/timed-out data is leniently treated as "unknown", never an
  automatic reject — only a value that IS present and clearly below its floor
  (`MIN_FUNDAMENTAL_SCORE`, `MIN_TECHNICAL_SCORE`, `MIN_MARKET_CAP_CR`) causes
  a skip-this-one-try-next-candidate outcome. If analysis-intelligence-service
  is down entirely, every check comes back all-None and every candidate passes
  through unfiltered — the scalp loop is never blocked by this being
  unavailable. Every check (pass or reject) is recorded on `ScalpCandidateLog`
  (new columns: `fundamental_score`, `technical_score`, `market_cap_cr`,
  `has_positive_catalyst`) for a full audit trail of what was known about a
  symbol at decision time.
- **Live Dhan order tracking** — new `GET /dhan/live-orders`, calling the
  already-existing `dhan_client.get_super_order_list()` (previously only used
  internally by `orders/reconcile.py`) and returning the raw broker-side
  super-order book, filtered to orders tagged `"SCALP"`. This is broker-side
  truth, independent of `/positions` (this service's own DB view, which can
  lag by up to one reconciliation cycle) — exactly the "live Dhan page
  activity" real-trade-service already has, adapted for this service's own
  order tagging.
- **Trade history dashboard** — new `GET /trades/history` (paginated, optional
  `status_filter`) returning both a summary (`total_trades`, `wins`, `losses`,
  `win_rate_pct`, `total_pnl`, `best_trade`, `worst_trade`) and the full trade
  list with **both** `entry_price` and `exit_price` per trade — the old
  `/positions` endpoint never actually returned `exit_price` at all despite
  the column existing since session 1; that gap is fixed too (added to
  `/positions` as well, and to the frontend's `PositionRow`, which previously
  only showed "Entry" with no sell-price counterpart).
- Frontend (`PositionStocksTab.tsx`): Auto-Pilot status tile (6th tile in the
  top strip) + Enable/Disable buttons, a "Run Cycle Now" button with a
  last-cycle-result banner, a full Trade History section (summary cards + a
  sortable-by-eye table with Buy Price/Sell Price/Qty/P&L/Status/Closed
  columns), and a Live Dhan Order Activity panel (own manual refresh button,
  polled independently and less aggressively than the rest of the dashboard —
  every 30s vs. 15s — since it hits Dhan's own API directly rather than this
  service's DB).
- **Scoped out of this pass** (flagged, not silently dropped): live
  fundamental/technical/news signals are NOT shown next to candidates in the
  `/candidates` screener view — doing so would mean running the quality gate
  against every scanned symbol instead of just the top few, which would
  violate the "quick" requirement. They're currently only visible after the
  fact via `ScalpCandidateLog` (not yet exposed through its own endpoint —
  see STATUS.md's Next Steps for the natural follow-up: a `GET
  /candidates/log` endpoint would surface this cheaply, since it's a DB
  read with no external calls).

### 3.14 Full audit pass — bug found, dead code cleaned, a "ghost feature" built for real (session 7)
User asked to check every wiring/mapping and find any open or incomplete code.
Ran `pyflakes` across the entire codebase of both position-stocks-service and
real-trade-service (not just files touched this session), plus manual review
of anything it couldn't catch (logic bugs, doc-vs-code mismatches).

- **Real bug found and fixed: `resilience/circuit_breaker.py`'s reset never
  actually reset.** `is_open()` only declared `global _open_since`, not
  `_failure_count` — so the line `_failure_count = 0` inside it created a
  local variable shadowing the module-level counter instead of resetting it.
  Effect: after the 60s reset timeout elapsed, `is_open()` correctly returned
  `False` once, but the stale failure count (already at/above threshold)
  never cleared — a single subsequent failure would immediately re-trip the
  breaker instead of needing 5 fresh consecutive failures, defeating the
  "half-open, try again" design. Fixed by adding `_failure_count` to the
  `global` declaration.
### 3.15 Dashboard sub-tabs, `/candidates/log`, liquidity-gate bug fix (session 12)
See the session-12 changelog entry below for full detail — summarized here
for the architecture record: frontend reorganized into 6 sub-tabs (no
behavior change, pure grouping); `screening/engine.py`'s liquidity gate
fixed (`pass` → `continue`, it was never actually skipping low-activity
symbols despite §3.3 documenting it as a real gate); `GET /candidates/log`
built (STATUS.md next-step #9, now done).

- **Dead code cleanup (pyflakes-driven, both services):** unused imports in
  `main.py` (`os`, `typing.List`), `screening/engine.py` (`time`),
  `execution/dhan_client.py` (`config`, plus a dead `global` declaration on a
  read-only variable), `feed/angelone_session.py` (`os`), `feed/scrip_master.py`
  (`config`), and a parsed-but-discarded `sub_type` byte in
  `feed/ws_client.py`'s frame parser (commented out to match the existing
  pattern for other intentionally-unused parsed fields nearby). Both services'
  entire codebases now pass `pyflakes` with zero findings.
- **Significant finding: the §3.8 shared Dhan order-rate guard was pure
  documentation — no code ever existed.** This doc and STATUS.md described
  `capital/shared_order_budget.py`, a `stockky_shared_order_budget` table, and
  wiring into both services' entry/exit paths as built since an earlier
  session ("built this session, no longer deferred" — §5's old item 7). A
  repo-wide search turned up **zero matches** for `shared_order_budget` in
  any `.py` file on either service — the documentation had drifted ahead of
  the actual code, describing a real-money safety feature that was never
  written. Built for real this session, matching the original design as
  closely as possible:
  - New `SharedOrderBudget` model (table `stockky_shared_order_budget`,
    deliberately not prefixed `scalp_`/`trade_` since it's explicitly shared)
    added to **both** services' `models.py` — same table, same columns, each
    service's own `Base`/`create_all()` (safe: whichever boots first creates
    it, the other just uses it).
  - `position-stocks-service/capital/shared_order_budget.py` and
    `real-trade-service/execution/shared_order_budget.py` — duplicated logic
    (not imported across services, same isolation rationale as everywhere
    else), fail-open on any DB error, one read+upsert per call.
  - **position-stocks-service:** gated `check_and_reserve()` call in
    `orders/entry.py`, positioned right before the actual Dhan Super Order
    call (deliberately NOT at the earlier gate checks — max-positions,
    capital, security-resolution can all still reject a candidate for
    unrelated reasons, and none of those should count against the shared
    budget). Unconditional `record_order_unconditional()` call in
    `orders/eod_squareoff.py`'s forced-exit path. New `shared_order_budget`
    field on `/status`.
  - **real-trade-service:** gated check in `manual_engine.py`'s manual REAL
    BUY path only (right before `dhan_client.place_order`, raising into the
    existing except-block so a budget-exhausted rejection looks like any
    other clean manual-BUY failure) — the automatic entry path
    (`entry_engine/entry.py`) is intentionally NOT gated by this, matching
    the original §3.8 design, which never mentioned it. Unconditional record
    in `exit_engine.py`'s `_send_real_sell` (the one function both AUTO and
    manual sells already share, matching that codebase's existing "exits
    always allowed" convention).
  - `SHARED_DAILY_ORDER_BUDGET` (default 5000) added to both services'
    `config.py`, with an optional override documented in `docker-compose.yml`.
  - **Confirmed NOT needed:** `orders/reconcile.py` (position-stocks-service)
    doesn't place any Dhan orders itself (pure polling/DB-status-update), so
    no wiring belongs there despite the doc's general phrasing suggesting
    "manual kill-switch closes" should record — that phrase describes
    real-trade-service's own manual-close behavior, not this service's
    simpler `/kill` endpoint (which only disarms + trips a flag, placing no
    orders of its own).
- **Verification:** `pyflakes` clean on both services' entire codebases;
  `py_compile` clean on every touched file in both services; real-trade-
  service's touched files (`models.py`, `config.py`, `manual_engine.py`,
  `exit_engine/exit.py`, new `shared_order_budget.py`) compile cleanly too —
  handled carefully given that service trades real money live. Not yet
  exercised against a live boot on either service — this is new code, same
  as everything else awaiting session 5's nginx fix + a live redeploy.

---

## 4. DB objects (built)
- `ScalpPosition` — open/closed scalp trade records, separate from `real-trade-service`'s `TradePosition` table.
- `ScalpCapitalLedger` — this pool's allocated/available cash, independent of the core system's ledger.
- `ScalpCandidateLog` — audit trail of scanned candidates and why each was taken/skipped.
- `ScalpGateState` — armed/disarmed, kill-switch, first-live-order-done, EOD-fired-date, daily order count.
- `stockky_shared_order_budget` — cross-service Dhan order-rate counter (§3.8), lives outside both services' own `models.py`.
- All writes async/non-blocking — DB slowness may delay logging, never a trade decision.

---

## 5. Implementation steps — status
1. ✅ Scope `position-stocks-service` folder structure + docker-compose entry.
2. ✅ Angel One true-WS client.
3. ✅ Dhan symbol→security_id side-map (duplicated cache logic).
4. ✅ Rolling-window screening engine + composite ranking.
5. ✅ Super Order integration (entry + target + SL), min-qty-first-live-order safety valve.
6. ✅ `ScalpCapitalLedger` + 50/50 enforcement logic.
7. ✅ 3:00 PM square-off sweep. Exit-reconciliation monitor (`orders/reconcile.py`) also built (polls Dhan's super order book for TARGET_LEG/STOP_LOSS_LEG fills, closes positions + releases capital automatically). Shared Dhan order-rate guard (§3.8) — **built this session**, no longer deferred.
8. ✅ "Position Stocks" frontend tab.
9. ✅ Risk-per-trade % — **confirmed by user: 2%.**
10. ✅ Master module enable/disable toggle (§3.11) — `service_enabled`, `/service/enable`,
    `/service/disable`, frontend Module tile + buttons.
11. ✅ 1-minute screening window (§3.12) — 4th window alongside 5m/15m/60m, own threshold,
    wired through screener + frontend.
12. ✅ Auto-Pilot toggle + manual "Run Cycle Now" (§3.13) — `auto_pilot_enabled`,
    `/autopilot/enable`, `/autopilot/disable`, `/cycle/run`, shared `_run_cycle()`.
13. ✅ Quality gate + live Dhan orders + trade history dashboard (§3.13) —
    `screening/quality_gate.py`, `/dhan/live-orders`, `/trades/history`, and the
    matching frontend sections.
14. ✅ Full audit + shared Dhan order-rate guard built for real (§3.14) — circuit
    breaker reset bug fixed, dead code cleaned repo-wide, `SharedOrderBudget`
    actually implemented on both services after being pure documentation for
    several sessions. No open items left in §5.

---

## 6. How to resume this if context resets
Attach this document **plus the latest Stockky repo zip** in a new conversation. Everything needed to continue — decisions made, code already verified (file/line references above), and exactly what's still open — is captured here.

**`services/position-stocks-service/STATUS.md` inside the zip is the live,
more-detailed progress tracker** (file-by-file, exact open items, prioritized
next-steps list) — read that first, this document second for architecture
rationale. Both travel together in every zip from now on.

---

## 7. Deploy issues found & fixed (session log)

- **2026-09-12, session 3 — `python-oracledb` invalid package name.**
  `services/position-stocks-service/requirements.txt` pinned
  `python-oracledb>=2.2.0` — not a real PyPI package (pip: "Could not find a
  version that satisfies the requirement... versions: none"). The correct
  package (and import name, used correctly everywhere in `oracle_compat.py`)
  is `oracledb`. Fixed to `oracledb==2.5.1`, matching the exact pin every
  other Stockky service already uses. Confirmed via grep this was the only
  occurrence of the wrong name anywhere in the repo. Deploy target is an
  Oracle Cloud VM on **aarch64/ARM** — worth remembering for any future new
  service's requirements.txt, since not every PyPI package ships aarch64
  wheels (this one does, once named correctly).

- **2026-09-12, session 3 (same day, later) — missing Oracle wallet volume
  mount in docker-compose.yml.** Container started, connected to
  `oracle_compat`'s DSN builder fine, but crashed on the very first DB call
  (`init_tables()`) with `DPY-4026: file '/oracle_wallet/tnsnames.ora' is
  missing or unreadable`. Root cause: `position-stocks-service`'s
  docker-compose block never got the
  `${ORACLE_WALLET_HOST_DIR:-./oracle_wallet}:/oracle_wallet:ro` volume
  mount that every other backend service has — so `/oracle_wallet` existed
  as an empty path inside the container with no wallet files in it at all.
  `oracle_compat.py` was already resolving `ORACLE_WALLET_DIR`/`TNS_ADMIN`
  to the correct path (confirmed from the traceback), it just had nothing
  real mounted there. Fixed: added the volume mount, plus explicit
  `ORACLE_WALLET_DIR=/oracle_wallet` and `TNS_ADMIN=/oracle_wallet` env vars
  (matching every other service's block exactly, rather than leaving it to
  depend on `.env` happening to define those container-internal-path keys).
  Also took the opportunity to set `RISK_PER_TRADE_PCT=2.0` /
  `RISK_PER_TRADE_PCT_CONFIRMED=true` directly in the compose block instead
  of leaving them as commented-out placeholders.

- **2026-09-12, session 5 — missing nginx reverse-proxy route (first live
  deploy attempt).** User deployed and reported the Position Stocks tab
  looked entirely broken: buttons appeared disabled, a red banner showed
  nginx's own `405 Not Allowed` error page verbatim, and the whole status
  strip read `DISARMED`/`CLOSED`/`DOWN`/`PAUSED` plus a stale
  "RISK_PER_TRADE_PCT not confirmed" warning, all simultaneously, on first
  load. Root cause: `deploy/nginx-stockky.conf` (the VM-level reverse
  proxy) was never updated when `position-stocks-service` was built — it
  only ever had `location` blocks for `/` (frontend), `/api/`
  (api-gateway :8000), and `/realtrade/` (real-trade-service :8005). With
  no `/positionstocks/` block, requests fell through to `/` — the
  frontend's own static-file server — whose default nginx handler only
  serves `GET`/`HEAD`. Every `POST` (`/arm`, `/disarm`,
  `/service/enable`, `/service/disable`, `/kill`, `/ledger/sync`,
  `/reconcile`) hit that handler and got the stock `405 Not Allowed` page
  back — the exact banner reported. The `GET` calls (`/status`,
  `/positions`, `/candidates`, `/ledger`) instead fell through to the
  SPA's `index.html` (200 OK but HTML, not JSON), so `/status` never
  returned real data — which is why every status-derived tile and the
  risk-confirmation banner looked "off" at once: one root cause surfacing
  in five places, not five separate bugs. Fixed: added a
  `stockky_position_stocks` upstream (`127.0.0.1:8006`) and a
  `location /positionstocks/` block to `deploy/nginx-stockky.conf`,
  mirroring `/realtrade/`'s exact pattern. **Requires the user to reload
  nginx on the VM and update the Position Stocks Settings URL to
  `https://stockky.duckdns.org/positionstocks`** — not yet re-verified
  against a live reload.

- **2026-09-13, session 10 — dashboard parity with Real Automatic Trade
  (Dhan Account card, Arming Sequence, Risk Configuration).** User asked
  for the Position Stocks tab to show the same Dhan-account-level detail
  Real Automatic Trade's Overview tab shows (screenshotted). Added:
  - `auth/dhan_credentials_ro.py`: new read-only `connection_status(db)` —
    verbatim mirror of real-trade-service's `dhan_credentials.connection_status`
    (masked client ID, token issued/expiry, hours/days/seconds remaining,
    24h hard-cap clamp). Still never writes to `trade_credentials` — see
    the file's existing module docstring for why that stays exclusively
    real-trade-service's job.
  - `main.py`: new `GET /dhan/account` (admin-only) — same response shape
    as real-trade-service's `/dhan/account` (connection/token status +
    a live `dhan_client.get_funds()` call, funds failing doesn't hide
    connection state). `GET /status` extended with
    `max_daily_loss_pct_of_pool`, `max_concurrent_scalp_positions`,
    `scalp_pool_capital_share_pct` for a Risk Configuration card.
  - Frontend (`PositionStocksTab.tsx` + `positionStocksApi.ts`): new
    `DhanAccountStatus` type + `dhanAccount()` call; three new cards —
    Arming Sequence (4-step checklist: admin authenticated / Dhan
    connected / risk config confirmed / armed — matching Real Automatic
    Trade's `GateStep` visual language), Dhan Account (client ID, live
    ticking token countdown, funds grid: Available/Utilized/Withdrawable/
    SOD Limit/Collateral/Blocked), and Risk Configuration (read-only —
    unlike Real Automatic Trade's, these knobs are env/config-driven here,
    not an editable DB row, so the card says so and points at the four
    env vars to change instead of rendering input fields for values that
    would just 404/no-op on save).
  - **Deliberately NOT ported:** CDSL eDIS verification. That flow exists
    to authorize delivery holdings for sale; this service places intraday
    MIS scalp orders, which don't go through that CDSL check the same way
    real-trade-service's delivery sells do — adding a decorative eDIS badge
    here would show status for a check this module's order flow doesn't
    actually depend on. Flagged for the user to confirm; easy to add later
    if scalp exits ever need it.
  - **Re: the ledger `/ledger` 500 (`AttributeError:
    'ScalpCapitalLedger' object has no attribute
    'daily_loss_kill_switch_tripped'`) in the container log the user
    attached this session:** confirmed NOT a live bug — session 9's fix
    (the column added to both `models.py` and `_COLUMN_MIGRATIONS` in
    `db.py`) is already present and correct in the code the user is
    running. The log's timestamps (13 Sep, same day) show it was captured
    against a container that hadn't yet been rebuilt/redeployed from that
    fix. No code change needed here; redeploying this zip's
    `position-stocks-service` resolves it.
  - **Verification:** `py_compile` + `pyflakes` clean across every `.py`
    file in `position-stocks-service` (not just touched files). Frontend:
    `npx tsc --noEmit` clean across the ENTIRE frontend project (not just
    `PositionStocksTab.tsx`/`positionStocksApi.ts`) — first time this
    service's session log has run a full project-wide type-check rather
    than just compiling the touched files.

- **2026-09-13, session 12 — sub-tabs, `/candidates/log`, and a real
  liquidity-gate bug found by the audit pass.**
  - **Frontend sub-tabs.** `PositionStocksTab.tsx` was one continuous scroll
    (Admin auth → Arming Sequence → Dhan Account → status grid → System
    Health → Risk Configuration → banners → action buttons → capital pool
    → Live Screener → Open Positions → Closed Today → Trade History → Live
    Dhan Orders → Settings). Admin auth and the critical banners (kill-switch
    tripped, risk-not-confirmed, fetch error) stay above the tabs since they
    matter regardless of what you're looking at; everything else is now
    grouped into six tabs: **Overview** (arming sequence, Dhan account,
    status grid, system health, risk config, status banners, last-cycle
    banner, action buttons, capital pool card), **Screener** (live screener
    + new Candidate Log), **Positions** (open + closed today),
    **Trade History**, **Dhan Live Orders**, **Settings**. Pure UI
    reorganization — no section's content or polling behavior changed, they
    just render conditionally on `subTab` instead of all at once.
  - **Real bug found (audit pass): `screening/engine.py`'s liquidity gate
    never gated anything.** §3.3 of this doc documents "min liquidity/avg
    volume" as one of the screener's gates. The code computed
    `tick_count < max(1, int(config.MIN_AVG_VOLUME / 5000))` correctly, but
    the body of that `if` was a bare `pass` with a comment "skip strict gate
    for now — log-only until calibrated" — it didn't even log, and every
    symbol proceeded to the window checks regardless of tick activity. Same
    shape as session 7's shared-order-budget finding: a gate described as
    existing in the tracking doc with no actual enforcement behind it. Fixed
    by changing `pass` to `continue` so a symbol below the liquidity floor
    is actually skipped (all four windows) rather than just noted and
    ignored.
  - **Built `GET /candidates/log`** — STATUS.md's Next Steps #9 flagged this
    since session 6 as a "natural follow-up, not yet built... a pure DB read
    (no external calls), cheap to add whenever it's wanted next." Added: a
    paginated (`limit`, default 100, max 500) read of `ScalpCandidateLog`
    ordered newest-first, optional `decision_filter` ("ENTERED"/"SKIPPED"),
    returning symbol/window/pct_change/composite_score/decision/reason plus
    the session-6 quality fields (fundamental_score, technical_score,
    market_cap_cr, has_positive_catalyst). Frontend: new `candidatesLog()`
    client method + `ScalpCandidateLogRow` type in `positionStocksApi.ts`,
    and a "Candidate Log — Why Entered / Skipped" table on the Screener
    sub-tab, polled on the same 15s cadence as the rest of the dashboard.
  - **Audit scope covered, nothing else found:** `py_compile` across every
    `.py` file in the service (clean), a manual AST-based unused-
    import/undefined-name pass (only false positives on `from __future__
    import annotations`, same as pyflakes would report — no `pyflakes`
    binary available in this sandbox, network disabled), and a manual
    read-through of `main.py` (routes/lifespan/trading loop), `models.py`
    vs `db.py`'s `_COLUMN_MIGRATIONS` (re-confirmed the session-11 pairing
    is still consistent — all 10 entries have a matching Column), `orders/
    entry.py`, `orders/adaptive.py`, `orders/reconcile.py`, and `screening/
    quality_gate.py`. All wired correctly; no other open/empty/dead code
    found this session.
  - **Frontend verification:** real npm install wasn't reachable this
    session (registry returned 403 — sandbox networking differs run to
    run), so verification fell back to: `esbuild` (bundler, present in this
    sandbox) compiling `PositionStocksTab.tsx` standalone with zero errors,
    plus an isolated `tsc --noEmit` pass on both `PositionStocksTab.tsx` and
    `positionStocksApi.ts` — clean except the expected isolated-compile
    artifacts (missing `react`/`../positionStocksApi` module resolution
    outside the real project, the `key`-prop false positive already
    documented in session 3's notes, and `import.meta.env` needing vite's
    client types) — no structural or logic errors. Recommend a real `npm
    install && npm run build` on the VM before/after deploy same as always.

- **2026-09-13, session 11 — the REAL root cause of the `/ledger` 500
  (`AttributeError: 'ScalpCapitalLedger' object has no attribute
  'daily_loss_kill_switch_tripped'`), finally fixed.** Session 10 claimed
  this was just a stale/pre-redeploy log — that was WRONG. The user
  redeployed session 10b's zip (confirmed by the screenshot: the new Dhan
  Account / Arming Sequence cards were rendering) and got the exact same
  traceback, live. Root cause: session 9's earlier "fix" only added
  `daily_loss_kill_switch_tripped(_date)` to db.py's `_COLUMN_MIGRATIONS`
  (which ALTERs the DB table) — it never actually added these two fields
  as `Column(...)` attributes on the `ScalpCapitalLedger` class in
  `models.py`. A database-level ALTER TABLE does nothing for a Python
  ORM instance's attributes; those come from the mapped class definition
  SQLAlchemy generates at import time, not from reflecting the live table.
  So `row.daily_loss_kill_switch_tripped` in `capital/ledger.py` (used in
  `reserve_capital`, `release_capital`, `reset_daily`, `get_state`) was
  ALWAYS going to raise `AttributeError` no matter how many times the DB
  column got migrated or the container got redeployed — the column
  existing in Postgres/Oracle was never the missing piece. Fixed by
  actually adding both fields to `ScalpCapitalLedger` in `models.py`
  (`Boolean`/`String(10)`, matching db.py's DDL types exactly). Also
  wrote a small `ast`-based script and ran it against every
  `_COLUMN_MIGRATIONS` entry vs. every model class to confirm this exact
  class of bug (migration-only field with no matching ORM Column) isn't
  hiding anywhere else in this service — all 10 entries now check out
  clean. Lesson for future sessions: `_COLUMN_MIGRATIONS` and the model
  class are two separate places that must BOTH be updated for a new
  field — a migration-only or model-only change looks fine in isolation
  and passes `py_compile`/`pyflakes` either way, but only fails at
  runtime the moment that specific attribute is actually touched.

- **2026-09-13, session 13 — 3 real bugs found in the previous session's
  audit but never actually written into any file/zip; implemented +
  verified here, plus a clean continued audit.**
  - **`feed/ws_client.py`'s `ws_status()` never reported connection state.**
    It returned `{running, subscribed_symbols, task_done}` — but the
    frontend (`positionStocksApi.ts`'s `WSStatus` type / the "WS Feed" card
    in `PositionStocksTab.tsx`) has always read `connected`, `last_tick_at`,
    `reconnect_attempts`, none of which existed. `running` only reflects
    that `start()` was called once and stays `True` through every
    disconnect/backoff cycle, so the LIVE/DOWN badge was reading an
    always-undefined field → always rendered DOWN, with no reconnect
    counter or last-tick timestamp ever shown. Fixed: added `_connected`,
    `_reconnect_attempts`, `_last_tick_at` module state, wired through
    `_ws_loop()` (set True + reset counter on successful connect; set False
    + increment counter on every disconnect/backoff/retry branch,
    including the "AngelOne session not ready" path), updated `stop()`,
    rewrote `ws_status()` to emit all fields the frontend expects
    (`last_tick_at` as an ISO-8601 UTC string via `datetime.fromtimestamp`).
  - **`capital/ledger.py`'s `reset_daily()` was dead code — never called
    anywhere in the service.** No scheduler, no startup hook, no admin
    route. Consequence: once the daily-loss kill switch tripped it stayed
    tripped forever, and `realized_pnl_today` accumulated across every
    day after the first trip instead of resetting — a real-money
    correctness bug. Fixed with a lazy reset-on-IST-date-change check
    (`_maybe_lazy_reset_daily`, called from `_get_or_create()` so every
    ledger read/write self-heals, even if the service was down across
    midnight — no scheduler dependency). Backed by a new
    `pnl_last_reset_date` column (`models.py` + `db.py`'s
    `_COLUMN_MIGRATIONS`) that tracks the last IST date the daily fields
    were valid for. `reset_daily()` itself is kept as an explicit
    manual/admin trigger layered on top, not removed.
  - **Removed dead code:** `compute_position_value()` in `capital/ledger.py`
    — a stub that always returned `0.0` and was never called; the real
    sizing math lives inline in `reserve_capital()` (its own docstring
    already said so).
  - **`resilience/circuit_breaker.py` had the same class of bug as the WS
    status one.** `status()` returned `{open, failure_count, open_since}`
    but the frontend's `CBadge` component (typed in `positionStocksApi.ts`)
    has always expected `{state, consecutive_failures, failure_threshold,
    cooldown_s, seconds_until_retry}` — so the circuit-breaker badge on the
    dashboard has never rendered real state, only the `if (!cb)` fallback.
    Rewrote to emit that exact shape (matching real-trade-service's own
    `CircuitBreaker.to_dict()`). While rewriting, also fixed the same
    half-open re-arm bug real-trade-service's breaker documented fixing
    for itself: the old `is_open()` silently reset `_failure_count`/
    `_open_since` to 0 the instant the cooldown elapsed, so a FAILED
    half-open probe call started counting from zero again instead of
    re-opening the breaker — meaning a struggling upstream that kept
    failing every probe would only ever look "half_open" with
    `seconds_until_retry` stuck at 0, never re-open for another cooldown.
    Now a failure recorded while already tripped re-arms `_open_since` for
    a fresh cooldown window, matching the correct state machine
    (closed → open → half_open → closed-or-reopen).
  - **Continued the file-by-file audit** (`execution/dhan_client.py`,
    `capital/shared_order_budget.py`, `auth/dhan_credentials_ro.py`,
    `feed/angelone_session.py`, `feed/scrip_master.py`,
    `auth/admin_auth.py`, `orders/eod_squareoff.py`, `config.py`,
    `oracle_compat.py`, `tz_utils.py`) — all confirmed correctly wired,
    no further bugs found.
  - **Verification:** re-ran the session-11 `ast`-based `_COLUMN_MIGRATIONS`
    vs. model-class consistency check against all 11 entries (10 + the new
    `pnl_last_reset_date`) — clean. `py_compile` across every `.py` file in
    the service. Real Python imports (not just `py_compile`) of every
    touched module plus a full `import main` (FastAPI app construction) —
    all clean with the service's actual deps installed. Functional
    in-memory-SQLite tests: (1) ledger — reserve → release with a >4% loss
    trips the kill switch; rewinding `pnl_last_reset_date` to a past date
    and re-reading the ledger correctly auto-clears the kill switch and
    `realized_pnl_today` while leaving `available_capital` untouched; (2)
    circuit breaker — 3 failures trips OPEN, cooldown elapses to
    HALF_OPEN, a failed probe correctly re-OPENS for a fresh cooldown
    (not stuck), a subsequent successful probe correctly CLOSES. NOT
    live-tested against the real Angel One WS / Dhan account (market
    closed at time of writing) — see the Ubuntu verification commands
    provided alongside this zip for a market-closed, no-order-placement
    smoke test of the whole service.

- **2026-09-13, session 14 — 3 more real bugs found from an actual live
  deploy (session 13's fixes running against the real Angel One/Dhan
  connection, market closed).**
  - **WS reconnecting every ~123s, silently, no log line.** Root cause:
    the heartbeat sent `await ws.ping()` — a raw WebSocket protocol
    control frame — but AngelOne's feed gateway expects an
    application-level text `"ping"` message (their own reference client
    sends `wsapp.send("ping")`, not a protocol ping), so the pings never
    registered as keep-alive traffic server-side and the idle connection
    was dropped roughly every 2 minutes regardless. Fixed:
    `await ws.send("ping")` instead of `await ws.ping()`. Also fixed the
    silence itself — `async for message in ws` exits WITHOUT raising when
    the server closes cleanly (only genuinely abnormal closes raise
    `ConnectionClosed`), so the existing `except ConnectionClosed`/
    `except Exception` handlers never fired for this case; added an
    `else:` clause on the `async for` (fires when the loop ends without a
    `break`) that logs the clean-close case so this is never silently
    invisible again.
  - **`ScalpGateState.daily_loss_kill_switch_tripped` — a second, fully
    disconnected copy of the exact bug fixed on `ScalpCapitalLedger` last
    session, on a different table.** `entry.py`'s early gate check reads
    THIS copy (`ScalpGateState`, set by `/kill`), completely independent
    of the ledger's own copy my previous fix addressed. Nothing ever
    reset it — confirmed live: a fresh deploy came up with
    `daily_loss_kill_switch_tripped=True` on the gate (`/status`) while
    the ledger's copy was correctly `False` (`/ledger`) — the two had
    drifted, and the gate's copy would have permanently blocked every
    future entry (even after re-arming) until someone edited the DB by
    hand. Fixed with the same lazy reset-on-date-change pattern, applied
    to all three places that independently fetch `ScalpGateState`
    (`main.py::_get_gate`, `orders/entry.py::_get_gate_state`,
    `orders/eod_squareoff.py::_get_gate_state`) since there's no shared
    helper between them. No new column needed — `daily_loss_kill_switch_
    tripped_date` (already stored) doubles as the reset marker: only
    clears when that date isn't today, so a same-day manual `/kill` still
    correctly holds for the rest of the day. `is_armed` is untouched by
    this reset (confirmed by test — a re-arm still requires an explicit
    `/arm` call either way, this only clears the kill-switch flag itself).
  - Frontend needed no change for this: `PositionStocksTab.tsx` already
    ORs the two kill-switch fields together
    (`status?.daily_loss_kill_switch || ledger?.daily_loss_kill_switch_
    tripped`) — the dashboard was defensively correct, only the backend's
    "never resets" bug on the gate's copy was the actual problem.
  - **Verification:** functional in-memory-SQLite tests of the gate lazy
    reset confirmed for all 3 call sites, confirmed a same-day trip is
    correctly NOT cleared (that would defeat `/kill`'s purpose), confirmed
    `is_armed` isn't touched by the reset. Full re-run of `py_compile`,
    `import main`, and the `_COLUMN_MIGRATIONS` consistency check — all
    still clean (no new columns added this session). NOT yet re-verified
    against a second live deploy — do that next before trusting the WS
    heartbeat fix actually stops the ~123s reconnect cycle in practice.

- **2026-09-13, session 14 (continued) — heartbeat-frame-type fix alone did
  NOT stop the ~120s WS reconnect cycle; live logs proved it.** The user's
  second live test showed `WS: connected` → `WS: server closed the
  connection cleanly` at 15:17:53 (exactly 120s after 15:15:53) and again
  at 15:19:56 (exactly 120s after 15:17:56) — WITH the app-level
  `ws.send("ping")` heartbeat already active. That rules out "wrong
  heartbeat frame type" as the sole cause; a fixed, exact 120s interval
  strongly suggests AngelOne's server enforcing its own connection
  lifetime (plausibly specific to idle/no-tick-data connections while the
  market is closed) rather than anything reacting to our heartbeats.
  Rather than guess another fix blind, added real diagnostics: both the
  clean-close `else` branch and the `except ConnectionClosed` branch now
  log `ws.close_code`/`ws.close_reason` (or `e.code`/`e.reason`), which
  will show the actual reason AngelOne gives for the close next time this
  runs. Also tightened `ANGELONE_WS_HEARTBEAT_INTERVAL_S` from 25.0 to
  10.0 to exactly match AngelOne's own reference client
  (smartapi-python's `SmartWebSocketV2.HEART_BEAT_INTERVAL`) as a
  legitimate baseline correction — but flagged honestly to the user that
  this alone may not fully resolve it if the cause is server-side. Next
  live test should be read for the close_code/reason values, and ideally
  re-run during actual market hours (09:15–15:30 IST) — a connection
  carrying real tick data is a meaningfully different test than an idle
  one, and would definitively separate "AngelOne caps idle connections"
  from "still broken even with live data".
  - Also found two workflow (not code) issues in the user's own terminal
    session, unrelated to the fixes above: (1) `unzip`/`cp` both failed
    ("cannot find or open" / "cannot stat") because the zip wasn't at
    `~` when the script ran, yet the container still showed session14's
    new log line — meaning the deploy must have already succeeded in an
    earlier, unlogged run of the same script; going forward the deploy
    script should verify the zip exists AND verify the running
    container's actual file content (not just trust log output) before
    declaring success. (2) The user is running the example curl script
    with the literal placeholder `YOUR_ADMIN_PASSWORD` still in it, so
    `/auth/login` correctly 401s and returns `{"detail": ...}` — which
    has neither `token` nor `access_token`, hence the `KeyError`. This
    was never a wrong-JSON-key bug in the example script; it's the
    unsubstituted placeholder. Needs to be far more obvious in the next
    version of the test script (e.g. fail loudly with a clear message
    instead of a bare Python traceback).

- **2026-09-13, session 14 (RESOLVED) — the ~120s WS reconnect cycle is
  confirmed AngelOne server behavior, not a client bug.** Live logs with
  the close_code/reason diagnostics added earlier this session show:
  `close_code=1001, close_reason='Connection Idle Timeout'`, four times in
  a row, every ~120-123s, with `reconnect_attempts` staying flat (each
  cycle completes cleanly before the next status poll). This is AngelOne's
  server deliberately closing feed connections that carry no live tick
  data — i.e. specific to the market being closed — not anything client-
  side; neither heartbeat frame type nor interval was ever going to
  prevent it, since AngelOne isn't waiting on our heartbeats for this
  check. No further heartbeat/protocol changes made. The one change this
  round: downgraded this specific, now-confirmed-expected case (code=1001
  + reason exactly "Connection Idle Timeout") from `logger.warning` to
  `logger.info` in `feed/ws_client.py`, so normal off-hours operation
  doesn't read as a recurring alarm — any OTHER close code/reason, or this
  same one recurring once the market is open and ticks are flowing, still
  logs at `warning` and would be worth a fresh look.
  - Reconnect handling itself needed no changes — it was already correct:
    clean detection (once the close_code/reason logging existed to see
    it), fast 3s backoff, full resubscription to all 2678 NSE-EQ tokens
    every time, `_connected`/`reconnect_attempts`/`last_tick_at` all
    updated correctly through every cycle.
  - **Remaining open item, not a bug — just unverified:** whether this
    exact idle-timeout cycle also happens once the market is open and
    real ticks are flowing (a connection carrying live data may not be
    considered "idle" by AngelOne at all, which would mean this simply
    stops happening during 09:15-15:30 IST) or whether it keeps recurring
    regardless. Only a live-market-hours test settles this; nothing more
    to fix from here without that data point.
  - Also fixed in this session's exchange (not code, but worth recording):
    my own example test script used a bare top-level `exit 1`, which — when
    pasted directly into an interactive SSH session rather than run as a
    script file — terminated the user's actual SSH connection. Corrected
    to wrap the logic in a bash function using `return 1` instead, which is
    safe to paste directly.

- **2026-09-13, session 18 — full re-audit; found and documented several
  real fixes (session 16/17 era) that were made in code but never written
  up here or in STATUS.md; fixed one new gap.** Note: sessions 15-17's
  entries were never appended to this log (only to STATUS.md) — see
  STATUS.md's Session 17/16 sections for that work directly; this entry
  covers session 18 only.
  - **Documented (not new code, verified correct by reading it): the
    `orders/eod_squareoff.py` fix forcing `is_armed=True` on the EOD
    flatten-all closing SELL.** Before this fix (already present in the
    code, just undocumented), a disarmed service with open positions would
    silently fail every EOD closing SELL with `DhanNotArmedError` and leave
    real-money positions unflattened past 3pm — the exact "no exceptions"
    case this doc's §3.7 exists to prevent. This was the most safety-
    relevant undocumented fix found this session.
  - **Documented: the `iso_utc()` timestamp fix** across `/status`,
    `/positions`, `/trades/history`, `/candidates/log`, and `/ledger` in
    main.py and capital/ledger.py. Previously several DateTime fields
    serialized without a UTC offset, so the browser parsed them as local
    time — every dashboard timestamp was off by +5:30 (IST). Confirmed
    `iso_utc()` is now applied consistently at every call site that returns
    a DB-sourced datetime to the frontend.
  - **Documented: the `reserve_additional()` quantity-floor fix** (orders/
    entry.py + capital/ledger.py) and the **`/dhan/live-orders` tag-leak
    fix** (main.py) and the **gate-mirror-on-trip fix** (capital/ledger.py's
    `release_capital()`) — all confirmed correctly implemented, matching
    what STATUS.md's Session 17 section already described.
  - **New gap found and fixed:** `POST /ledger/reset-daily` existed on the
    backend with zero frontend wiring — no `positionStocksApi.ts` method,
    no button in `PositionStocksTab.tsx`. Added `resetLedgerDaily()` to the
    API client and a confirm-guarded "Reset Daily Ledger" button in the
    Overview tab's action row (same two-step confirm UX as Kill Switch).
  - **Re-confirmed clean, no changes:** screening/engine.py's liquidity
    gate, orders/adaptive.py, resilience/circuit_breaker.py's state machine
    + call sites, feed/ws_client.py's WS status + idle-timeout handling,
    feed/scrip_master.py, screening/quality_gate.py, auth/admin_auth.py,
    auth/dhan_credentials_ro.py, db.py's Oracle autoincrement backfill,
    oracle_compat.py. deploy/nginx-stockky.conf and docker-compose.yml's
    position-stocks-service block both still have the fixes from earlier
    sessions intact (spot-checked, not modified).
  - **Verification:** `py_compile` + `pyflakes` clean across every `.py`
    file in the service. AST-based `_COLUMN_MIGRATIONS` vs. `models.py`
    consistency check re-run against all 16 entries — clean. Real `npm
    install` (177 packages) + `npm run build` (`tsc && vite build`) against
    the real `@types/react`/Tailwind config — zero TypeScript errors, build
    succeeded.

- **2026-09-14, session 23 — full Position Stocks tab re-audit (main.py +
  frontend only, per user's scope note — execution/screening/capital/feed/
  resilience/auth not re-walked, already deep-audited sessions 19/19b-d/
  21/22).** 2 real frontend-only bugs found, neither safety-critical: (1)
  `PositionStocksTab.tsx`'s "Closed Today (N)" list filtered only on
  `status !== "OPEN"`, no date check — could show positions that actually
  closed on an earlier day, mislabeled as today's, since `GET /positions`
  just returns the last 50 rows. Fixed to filter each row's `closed_at`
  (IST calendar date) against today. (2) `armed_at` — returned by
  `GET /status` since early on, typed on `ScalpStatus`, but never rendered
  anywhere — same "built on backend, never wired to tab" pattern as
  session 6/18/19/22b's other findings. Added "Armed since `<time>`" to
  the Arming Sequence card. Also caught up STATUS.md with session 22b's
  order-id wiring work (done in code, never written up there). Verified:
  `py_compile` clean (no backend files touched), real `npm install` +
  `npm run build` — zero TypeScript errors.

- **2026-09-14, session 24 — full buy/sell order-placement audit.** Read
  `orders/entry.py`, `orders/adaptive.py`, `capital/ledger.py`,
  `execution/dhan_client.py`'s order calls, `orders/reconcile.py`, and
  `orders/eod_squareoff.py` end-to-end. Real bug found and fixed:
  `place_super_order()` only rounded the entry reference `price` to a
  valid tick for `order_type=="LIMIT"`, but this service always calls it
  with `order_type="MARKET"` and a raw, unrounded live LTP — and the
  function's own docstring says Dhan validates target/stop against this
  reference price regardless of order_type. `target_price`/`stop_price`
  were already tick-rounded via `orders/adaptive.py`; the reference price
  they're checked against wasn't, on the one order type actually used.
  Fixed to round unconditionally whenever `price` is non-zero. Flagged
  (not built — real-money feature, user's call): no manual single-
  position exit exists; `MANUAL_EXIT` is a declared status with zero code
  path. Everything else re-confirmed correct: adaptive levels formula,
  ledger reserve/release + daily-loss-trip math, reconcile.py's leg-price
  fallback chain, eod_squareoff.py's unconditional forced SELL.
  `get_positions()` confirmed dead code (declared, never called).
  Verification: `py_compile` clean on every file read/touched.

- **2026-09-14, session 25 — buy/sell re-audited for high-volatility/
  fast-moving-stock scenarios.** Real bug found and fixed: `pos.entry_price`
  was written once (to `candidate.current_ltp`, the scan-time LTP) in
  `orders/entry.py` and never corrected — every `realized_pnl` calc in
  `reconcile.py` uses it, so booked P&L was silently wrong by however much
  the real fill drifted from the scan-time snapshot. Real risk: up to 3
  candidates each go through `quality_gate.py`'s multi-second-timeout
  fundamental/technical/event checks, sequentially, before entry — several
  seconds of drift is normal, not an edge case, on the volatile names this
  strategy targets. Dhan's response already carries the true fill
  (`averageTradedPrice` on ENTRY_LEG — reconcile.py was already reading it
  for the exit-price fallback, never for the entry side). Fixed:
  `run_exit_reconciliation()` now corrects `entry_price` to the real fill
  as soon as it's confirmed traded, for OPEN and EOD_SQUAREOFF-pending
  positions alike (idempotent, capital ledger untouched). Flagged, not
  fixed: `capital_risked` has the same staleness (computed off the same
  scan-time LTP, never reconciled against the real fill cost) — fixing
  that means touching the shared ledger + kill-switch math, held for
  explicit sign-off. Confirmed target/stop levels are correctly NOT
  LTP-reactive post-placement (that's bracket-order behavior by design,
  not a bug). Verification: `py_compile` clean on `orders/reconcile.py`.

- **2026-09-14, session 26 — fixed the capital_risked staleness gap
  flagged (not fixed) at the end of session 25.** Added
  `capital/ledger.py::reconcile_position_cost(db, delta)`. `orders/
  reconcile.py`'s entry-price-correction block now also recomputes
  `capital_risked` as `quantity * real_entry_price` and pushes the delta
  through the ledger, so `available_capital` matches what the position
  will actually return at exit. Positive delta is allowed to push
  `available_capital` negative (honest signal of real Dhan overspend, not
  clamped) — same reasoning as `reserve_additional()`'s existing
  min-qty-floor shortfall handling. Verified with a real functional test
  (SQLite in-memory, mocked `get_super_order_list`): ₹100 estimated entry
  / ₹10,000 reserved / qty 100, real fill ₹103.50 → confirmed
  `capital_risked`→₹10,350 and `available_capital` drops by exactly ₹350;
  then TARGET_LEG fills at ₹106 → confirmed `capital_risked +
  realized_pnl` == `exit_price * quantity` == ₹10,600 exactly (true sale
  proceeds — previously under-returned by ₹350, permanently). `py_compile`
  clean on both touched files.

- **2026-09-14, session 27 — final open-items sweep.** Grepped the whole
  service for TODO/FIXME/"not yet"/"future improvement"/"ASSUMPTION
  FLAGGED" markers and re-checked every `main.py` route not yet
  individually re-verified this run (`/arm`, `/disarm`, `/service/*`,
  `/autopilot/*`, `/cycle/run`, `/kill`, `/ledger*`, `/ws-status`,
  `/dhan/*`, `/reconcile`) — all correct. Found and fixed one more small
  gap: `orders_placed_today` (this service's own daily order cap,
  `config.DAILY_ORDER_BUDGET`=300) was shown with no denominator, unlike
  `shared_order_budget` right next to it. Added `orders_placed_today_
  budget` to `GET /status` and matched the tile's display/color-cue
  pattern to the Shared Dhan Order Budget tile. Everything else the grep
  surfaced is already-known/already-flagged (reconcile.py's live-
  verification-needed leg-price assumption, the EOD plain-SELL exit-price
  placeholder, the still-pending manual-single-exit feature — awaiting
  user go-ahead). `get_positions()` reconfirmed dead code, untouched.
  Verification: `py_compile` clean on `main.py`; real `npm install` +
  `npm run build` — zero TypeScript errors.

- **2026-09-14, session 28 — real bug in WS tick buffer sizing.** Read
  `screening/engine.py` fully for the first time this series, traced into
  `feed/ws_client.py`'s tick storage. `_tick_buffers` was
  `deque(maxlen=3_600)` — a hard tick-COUNT cap explicitly assuming
  "1 tick/sec" per the module's own prior docstring. `_rolling_pct_change()`
  scans backward for a tick old enough to anchor each 1m/5m/15m/60m
  window and silently returns None if it can't find one — and a liquid,
  actively-moving stock (exactly this strategy's target) can push ticks
  well faster than 1/sec during a burst, draining the buffer's real time
  depth below 60 (or 15) minutes long before it fills on count. Fixed:
  every append now also prunes anything older than 65 minutes (margin
  over the longest window), same time-pruning pattern `_update_volume()`
  already uses; `_MAX_TICKS` kept only as a 50,000-entry memory-safety
  backstop. Verified with a functional test: sustained 10 ticks/sec for
  65 minutes — buffer now retains the full 65 min (39,000 entries) and
  the 60m window returns a real number instead of None (previously would
  have gone dark after 6 minutes of buffer depth). Deliberate scope
  exception: this touches `feed/`, which the user's session-22b scope
  note said was already deep-audited and not to re-walk — done because
  this session's request explicitly reopened the search and the bug is
  directly on-point for the high-frequency-price-change theme this
  thread has tracked throughout. `py_compile` clean on `feed/ws_client.py`
  and every file touched this whole thread, re-verified together.

- **2026-09-15, session 29 — `capital/shared_order_budget.py` given its
  first full read this series; real bug fixed in `screening/quality_gate.py`;
  full re-audit of the rest of the tab.**
  - **Fixed:** `screening/quality_gate.py::_fetch_fund_tech()` awaited the
    fundamental and technical HTTP calls one after another inside a single
    function, even though both the module docstring and `check()`'s
    docstring describe all three checks (fund/tech/event) as running
    concurrently — only the outer `_fetch_fund_tech(...)` vs.
    `_fetch_event_signal(...)` pair was actually parallel via
    `asyncio.gather`. Real effect: true worst-case wait per candidate was
    `fund_time + tech_time` (bounded together by `max(..., event_time)`),
    not `max(fund_time, tech_time, event_time)` — up to ~2x
    `QUALITY_GATE_TIMEOUT_S` of extra wall-clock time on a timeout, which
    directly compounds the scan-time-LTP entry-price staleness already
    tracked in sessions 25/26 (more time between the screener's price
    snapshot and the real fill, on top of up to 3 candidates each going
    through this gate sequentially). Split into `_fetch_fundamental()` /
    `_fetch_technical()` and gather them properly; `_fetch_fund_tech()` is
    now a thin `asyncio.gather` wrapper kept for the same call signature.
    No behavior change to the pass/fail logic itself, only latency.
  - **Reviewed, not changed:** `capital/shared_order_budget.py`. Logic,
    fail-open handling, and the `/status` wiring are all correct as
    documented. One thing worth flagging, not fixing without sign-off:
    `check_and_reserve()` is a plain read-then-increment (SELECT, compare,
    `+= 1`, commit) — not one atomic conditional UPDATE. Since this table
    is shared across two separate processes (this service and
    real-trade-service) hitting the same DB row, a call from each landing
    in the same few-millisecond window could both read the count before
    either commits, and both increment — the shared cap could be
    overshot by a handful of orders on a bad-timing day. Consistent with
    the module's own "soft governor, fail-open always, not a financial
    ledger" design intent (session 7), so left as-is; flagging in case
    real order volume ever makes a tighter atomic
    `UPDATE ... WHERE orders_placed_today < budget RETURNING ...` worth
    the added complexity.
  - **Re-audited fully, no changes needed:** `screening/engine.py` (full
    read — rolling pct-change window scan, liquidity gate, composite score
    formula, on-tick-hook registration ordering — all correct, ties out
    with session 28's ws_client fix cleanly); `execution/dhan_client.py`
    (full read — tick rounding, security-id cache/collision handling,
    Super Order placement incl. session 24's ref-price rounding fix and
    the `tag`-kwarg SDK-version fallback — all correct). Repo-wide grep for
    TODO/FIXME/"not yet"/ASSUMPTION markers surfaced nothing new (only the
    two already-known, already-flagged items in `reconcile.py` and
    `eod_squareoff.py`). Frontend cross-check: `orders_placed_today_budget`
    and `shared_order_budget` (session 27's additions) are correctly typed
    in `positionStocksApi.ts` and rendered in `PositionStocksTab.tsx` —
    no drift found.
  - **Verification:** `py_compile` clean on `screening/quality_gate.py`,
    `capital/shared_order_budget.py`, `screening/engine.py`,
    `execution/dhan_client.py`. `pyflakes` unavailable in this sandbox run
    (no registry egress) — not run this session; nothing in the diff is
    the kind of thing `py_compile` would miss (no new unused imports/names
    introduced).

- **2026-09-15, session 30 — full audit of `_run_cycle()` / `_trading_loop()`
  (the run-cycle pipeline), per explicit request.** 2 real bugs found and
  fixed, both in `main.py`.
  - **Bug 1 (real, safety-relevant): `circuit_breaker.is_open()` was
    checked in `_trading_loop()` BEFORE `_run_cycle()` was ever called —
    `if circuit_breaker.is_open(): continue` skipped the entire cycle,
    including exit reconciliation and EOD squareoff, both of which this
    doc has repeatedly documented elsewhere (§3.7, sessions 14/18) as
    "unconditional, no exceptions." A breaker trip (5 consecutive
    failures anywhere in `_run_cycle` — not necessarily Dhan-related,
    `record_failure()` fires on any exception) opens for
    `_RESET_TIMEOUT_S`=60s, longer if the half-open probe also fails —
    during that whole window, a real position whose TARGET_LEG/
    STOP_LOSS_LEG filled on Dhan's side would sit un-reconciled, and a
    breaker open at exactly 3pm would skip the EOD flatten-all sweep
    entirely. Fixed: moved the `is_open()` check out of `_trading_loop()`
    and into `_run_cycle()` itself, gating only the entry-attempt step
    (the one actually-risky Dhan call worth protecting) — reconcile/EOD/
    scan/quality-gate now run every tick regardless of breaker state; a
    tripped breaker shows up as `skipped_reason: "CIRCUIT_BREAKER_OPEN"`
    in the Run Cycle summary, same pattern as the existing
    SERVICE_DISABLED/NOT_ARMED/PAST_EOD_TIME reasons. Side benefit: manual
    `POST /cycle/run` now also respects the breaker for entry (it never
    did before), consistent with this function's own stated goal that
    AUTO and MANUAL "can never drift apart in behavior."
  - **Bug 2 (real, latency): the quality-gate loop over `top_n` candidates
    called `await quality_gate.check(candidate.symbol)` one at a time
    inside a plain `for` loop.** Even after session 29's fix made each
    individual check's fund/tech fetch concurrent internally, checking
    across candidates was still fully serial — worst case (first N-1
    candidates all reject) the cycle paid N × single-candidate latency
    before reaching entry, directly the cost session 25 flagged as the
    source of scan-to-fill price drift (corrected after the fact in
    sessions 25/26, never shortened at the source until now). Fixed:
    `asyncio.gather()` all `top_n` checks up front, then walk the results
    in the same priority order to pick the first passer — identical
    pass/fail logic and "only ever try one candidate per cycle" behavior,
    worst-case wait drops from sum(check_i) to max(check_i). Trade-off:
    always pays for `QUALITY_GATE_TOP_N` HTTP round trips now instead of
    stopping early — each is a short best-effort GET
    (`config.QUALITY_GATE_TIMEOUT_S`), small cost next to the latency win.
  - **Reviewed, no change — direct DB calls on the event loop:**
    `_run_cycle()`'s gate lookup (`db.query(ScalpGateState)...`), open-
    positions query, and the closing `db.commit()` all run synchronously
    on the event loop thread, not offloaded via `asyncio.to_thread` like
    the Dhan-calling steps were in an earlier session. This is the same
    pattern every other route handler in this service uses (sync
    SQLAlchemy directly in FastAPI's request path) and DB round trips are
    normally single-digit-to-low-double-digit milliseconds, not the
    multi-second Dhan calls that motivated the earlier `to_thread` fixes
    — not treated as a bug, just confirmed consistent rather than silently
    unexamined. Worth revisiting only if real DB latency (e.g. Oracle ADB
    wallet handshake overhead under load) turns out to be materially
    higher in practice.
  - **Re-confirmed correct, no changes:** manual `POST /cycle/run`'s
    `service_enabled`/`is_armed` gates and lock check;
    `_trading_loop()`'s admin-session-independence guarantee (session-N
    finding, re-read this session, still accurate); `record_success()`/
    `record_failure()` call sites — unaffected by the breaker-placement
    fix, still fire from the same places.
  - **Verification:** `py_compile` + `ast.parse` clean on `main.py`. Not
    run this session (sandbox has no live DB/Dhan/market to exercise
    against): a functional test of the new breaker-open branch — would
    need mocking `circuit_breaker.is_open()` True mid-cycle and confirming
    reconcile/EOD still execute and `attempt_entry` is never called.
    Recommend as a live/staging smoke check before relying on this branch
    under a real breaker trip.

## session112 round 30 (2026-09-25) — closed `tests/test_config_getters.py`'s last coverage gap
- Every test in this file started with `ADMIN_PASSWORD_HASH_B64`/`ADMIN_PASSWORD_HASH`
  genuinely unset, so `_restore_config_env`'s teardown (`if val is None: pop / else:
  os.environ[key] = val`) only ever took the `pop` branch — the `else` restore-a-real-value
  branch (line 93) was permanently unreachable by every existing test.
- Added a new fixture, `_preexisting_admin_hash_env`, that sets `ADMIN_PASSWORD_HASH_B64` to
  a real value *before* `_restore_config_env` snapshots it (ordered ahead of it in the new
  test's parameter list, so its setup runs first and its own teardown runs last/LIFO) and
  reloads `config` again in its own teardown so the module's final state still matches the
  truly-empty environment every other test in the suite expects — no isolation regression.
  New test: `TestAdminHashB64Decode::test_teardown_restores_a_preexisting_b64_env_value`.
  `tests/test_config_getters.py` now 100% (was 98%, missing line 93).
- Full suite re-run: 2280 passed (was 2279), still 99% total — the handful of remaining
  gaps (`test_db.py`, `test_dhan_client.py`, `test_edis_precheck.py`, `test_eod_squareoff.py`,
  `test_main.py`, `test_oracle_compat.py`, `test_screening_support.py`, `test_scrip_master.py`,
  `test_tz_utils.py`, `test_ws_client.py`, `test_ws_client_loop.py`,
  `test_ws_client_secret_redaction.py`) are next in line for a future round.
  Delivered: stockky-v2-main-2026-09-25-session112-round31-config-getters-coverage.zip.
