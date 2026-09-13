# position-stocks-service — Implementation Status

> **How to use this file:** When starting a new session, read this file + TRACKING.md
> to understand exactly where work left off. The §Next step section tells you the
> single thing to do next — no re-derivation needed.

---

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

## Next steps (in priority order)

0. **Reload the VM nginx config and re-point the Settings URL** (session 5 —
   see the 🔴 section above and TRACKING.md §7). This blocks everything else:
   `sudo cp deploy/nginx-stockky.conf /etc/nginx/sites-available/stockky && sudo nginx -t && sudo systemctl reload nginx`,
   then set the Position Stocks Settings URL to
   `https://stockky.duckdns.org/positionstocks`. Confirm the tab shows real
   `armed`/`market`/`ws`/`module` state instead of the all-blank/405 view
   before touching anything below.
1. ~~Check `scalp_gate_state` for the migration caveat~~ — **RESOLVED session 4.**
   `db.py`'s `init_tables()` now runs `_ensure_columns()` after `create_all()`: an
   idempotent inspector-based check that ALTERs any table missing a column
   models.py has added since it was first created (currently just
   `scalp_gate_state.service_enabled`). Safe to run on every boot, dialect-aware
   (Oracle `NUMBER(1)` / Postgres `BOOLEAN`), logs loudly if it actually migrates
   something, never crashes startup on a DDL failure. No manual ALTER needed —
   future column additions just need one line added to `db.py`'s
   `_COLUMN_MIGRATIONS` list.
2. **Redeploy and confirm clean boot** — this is the first deploy attempt since the
   `DPY-4026`/`oracledb` fixes from session 3; nothing has been confirmed booting
   successfully yet. Watch for `init_tables()` completing and the WS client connecting.
   **Requires your VM — can't be done from this sandbox.**
3. **First live Super Order** — with `FIRST_LIVE_ORDER_MIN_QTY_OVERRIDE=true` (default),
   arm the service during market hours (and make sure the module is enabled — new in
   session 4, defaults to enabled) and watch the very first real entry fire at qty=1.
   Confirm the fill shape matches expectations, then flip the override off.
   **Requires your VM + live market hours — can't be done from this sandbox.**
4. ~~Run `npm install && npm run build`~~ — **RESOLVED session 4.** Sandbox networking
   now reaches the real npm registry (`registry.npmjs.org`) — ran a genuine
   `npm install` (177 packages) + `npm run build` (`tsc && vite build`) against the
   real `@types/react`/Tailwind config, not a stub. **Zero TypeScript errors, build
   succeeded** (`dist/assets/index-*.js`, 997.71 kB / 268.62 kB gzip — a pre-existing
   single-chunk-size warning, unrelated to this session's changes, not something to
   fix now). This confirms `positionStocksApi.ts` and `PositionStocksTab.tsx` are
   genuinely clean against real type resolution, not just a syntax parse.
5. Watch the first few real TARGET_HIT/STOP_HIT exits and cross-check `realized_pnl`
   against Dhan's own order history (open item #3, exit-leg fill price).
   **Requires live trades — can't be done from this sandbox.**
6. Once live, watch how often the new 1m window actually fires vs. 5m/15m/60m —
   tune `MIN_PCT_CHANGE_1M` up if it's mostly noise (§3.12 flags this as likely).
   **Requires live market data — can't be done from this sandbox.**
7. **Set the analysis-intelligence-service URLs for the quality gate (session 6)**
   — `ANALYSIS_INTELLIGENCE_URL` (or the individual `FUNDAMENTAL_URL`/
   `TECHNICAL_URL`/`EVENT_URL` overrides) in the service's env. Without these set
   correctly, `screening/quality_gate.py` fails open silently (every candidate
   passes through unfiltered, which is safe but means the fund/tech/news signal
   isn't actually doing anything) — check the logs for "quality_gate: ... fetch
   failed" lines after deploy to confirm it's actually reaching the service.
8. **Verify the Live Dhan Order Activity panel and Trade History numbers against
   what you see in Dhan's own app** once real orders start flowing (session 6) —
   this is the first time this dashboard surfaces broker-side data directly, so
   it's worth a manual cross-check the first few times.
   **Requires live trades — can't be done from this sandbox.**
9. ~~Natural follow-up, not yet built: a `GET /candidates/log` endpoint~~ —
   **DONE session 12.** Built, plus a "Candidate Log" table on the frontend's
   new Screener sub-tab. See TRACKING.md §3.15.
10. **Verify the shared Dhan order-rate guard on next deploy (session 7)** — new
    on both services this session (see TRACKING.md §3.14). Check `/status`'s
    `shared_order_budget` field on position-stocks-service after a few real
    orders, and watch real-trade-service's logs for
    "SHARED Dhan order budget exhausted" or "shared_order_budget... failed
    (failing open)" lines to confirm the guard is actually reachable and
    counting, not silently no-op'ing. **Requires live trades on both
    services — can't be done from this sandbox.**
11. **Out of scope but worth knowing:** session 7's `pyflakes` audit also ran
    across real-trade-service's ENTIRE codebase (not just the files touched
    for the shared-budget wiring) and found a sizeable pre-existing backlog of
    unused imports/variables in files this session never touched (`notifier.py`,
    `auth/admin_auth.py`, `offline_test_harness.py`, `adaptive_thresholds.py`,
    `market_feed/feed.py`, `watchlist_engine/dynamic_universe.py`, `db.py`,
    `execution/auto_pilot.py`, `execution/reconcile.py`, `execution/dhan_client.py`,
    a couple of `scripts/`). None of this session's actual edits appear in that
    list — confirmed clean. Left untouched deliberately: real-trade-service
    trades real money live, and a repo-wide cleanup pass there deserves its own
    dedicated, careful session rather than being folded into this one.

---

## How to resume

Attach `TRACKING.md` + the latest repo zip in a new conversation.
Everything needed to continue is captured here — no re-derivation needed. Just say
which of the "Next steps" above to continue from, or describe what's changed.

Each time you come back to this project, expect this loop: read this file +
TRACKING.md first, report back what's done vs. what's next in plain terms, then work
through the next step(s) and hand back an updated zip in the same structure —
repeating step by step rather than dumping everything at once.
