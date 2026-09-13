# Session 6 (2026-09-12) — Auto-Pilot, manual cycle, quality gate, Dhan-live + trade history

Scope: `services/position-stocks-service/` + `docker-compose.yml` + frontend.
Full detail in TRACKING.md §3.13 and STATUS.md's completed-steps table (rows
12-15) — this is a pointer, not a duplicate.

## 1. Auto-Pilot toggle + manual "Run Cycle Now"
- `models.py`: `ScalpGateState.auto_pilot_enabled` (default True) — a third
  switch alongside `is_armed`/`service_enabled`. Screening always runs when
  armed+enabled+market-open; only the *automatic* entry is gated by this.
- `main.py`: refactored the trading loop body into a shared
  `_run_cycle(db, trigger)` used by both the 10s background loop
  (`trigger="AUTO"`) and the new `POST /cycle/run` (`trigger="MANUAL"`, which
  bypasses auto_pilot_enabled but still requires is_armed+service_enabled).
  `asyncio.Lock` prevents the two from overlapping.
- New: `POST /autopilot/enable`, `POST /autopilot/disable`.

## 2. Quality gate — fundamental/technical/news/bulk-deal pre-check
- New `screening/quality_gate.py`. Applied ONLY to the top
  `config.QUALITY_GATE_TOP_N` (default 3) price/volume-ranked candidates per
  cycle — never the whole scan universe, to stay fast.
- Reuses analysis-intelligence-service's existing `/technical/analyze/{symbol}`,
  `/fundamental/analyze/{symbol}`, `/event/events/{symbol}/categorized`
  endpoints (same ones real-trade-service's own quality gate already calls) —
  a shared, read-only backend, not real-trade-service's own state.
- Short timeout (2.5s default), fully fail-open: missing/timed-out data never
  rejects a candidate on its own; only a value that IS present and below its
  floor (`MIN_FUNDAMENTAL_SCORE`/`MIN_TECHNICAL_SCORE`/`MIN_MARKET_CAP_CR`)
  causes a skip-and-try-next-candidate outcome.
- `ScalpCandidateLog` gets 4 new nullable columns recording what was checked,
  win or skip, for a full audit trail.
- `docker-compose.yml`: added `TECHNICAL_URL`/`FUNDAMENTAL_URL`/`EVENT_URL`
  env vars (same values every other service already uses) and
  `analysis-intelligence-service` to `depends_on`.

## 3. Live Dhan order tracking
- New `GET /dhan/live-orders` — calls the already-existing
  `dhan_client.get_super_order_list()` (previously internal-only, used by
  `orders/reconcile.py`), filtered to `tag="SCALP"`.

## 4. Trade history dashboard
- New `GET /trades/history` — summary stats (win rate, total P&L, best/worst
  trade) + full trade list with `entry_price` AND `exit_price`.
- Bug found and fixed in passing: `/positions` never returned `exit_price` at
  all despite the column existing since session 1 — added there too, and to
  the frontend's `PositionRow` (which previously only showed "Entry", no
  sell-price counterpart).

## 5. Frontend (`PositionStocksTab.tsx`, `positionStocksApi.ts`)
- Auto-Pilot status tile (top strip widened to 6 tiles) + Enable/Disable
  buttons, "Run Cycle Now" button + result banner, last-cycle-run timestamp.
- New Trade History section: 4 summary cards + a table (Symbol/Window/Buy
  Price/Sell Price/Qty/P&L/Status/Closed).
- New Live Dhan Order Activity panel: own manual refresh + independent 30s
  poll (slower than the rest of the dashboard's 15s, since this hits Dhan's
  API directly rather than this service's own DB).

## Explicitly scoped out (flagged, not silently dropped)
Live fundamental/technical/news signals are NOT shown next to candidates in
the `/candidates` screener view — that would mean running the quality gate
against every scanned symbol instead of just the top few, defeating the
"quick" requirement. Currently only visible after the fact via
`ScalpCandidateLog`. A cheap `GET /candidates/log` endpoint (pure DB read, no
external calls) would surface this — noted in STATUS.md's Next Steps as the
natural follow-up whenever it's wanted.

## Verification done in sandbox
- All touched Python files: `py_compile` clean.
- `docker-compose.yml`: parsed with PyYAML, structure intact.
- Frontend: real `npm install` (registry reachable) + `npm run build`
  (`tsc && vite build`) — **zero TypeScript errors**, build succeeded.
- NOT verified: actual live calls to analysis-intelligence-service or Dhan's
  super-order-list endpoint (both require the live VM) — the fail-open design
  means a misconfigured URL degrades gracefully (quality gate no-ops) rather
  than breaking anything, but the *positive* signal itself is unverified.
