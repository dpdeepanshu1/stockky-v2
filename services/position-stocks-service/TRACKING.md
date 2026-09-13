# Position Stocks — Project Tracking Document
**Purpose of this doc:** continuity anchor. If chat context/limits reset, attach this doc + the latest Stockky zip in a new conversation and work continues from exactly here — nothing re-derived from scratch.

**Last updated:** 2026-09-12 (session 7)
**Status:** Session 7 was a full audit pass across BOTH position-stocks-service
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
