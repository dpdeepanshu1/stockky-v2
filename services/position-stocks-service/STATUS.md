# position-stocks-service — Implementation Status

> **How to use this file:** When starting a new session, read this file + TRACKING.md
> to understand exactly where work left off. The §Next step section tells you the
> single thing to do next — no re-derivation needed.

---

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

---

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

## Open items

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

1. **First live Super Order** — with `FIRST_LIVE_ORDER_MIN_QTY_OVERRIDE=true` (default),
   arm the service during market hours and watch the very first real entry fire at
   qty=1. Confirm the fill shape matches expectations, then flip the override off.
   This is the only step left that genuinely requires the live deployed VM + market
   hours — it can't be done from a sandbox.
2. Run `cd frontend && npm install && npm run build` once on the VM to get a real,
   fully-resolved build (belt-and-suspenders after session 3's syntax/type checks).
3. Watch the first few real TARGET_HIT/STOP_HIT exits and cross-check `realized_pnl`
   against Dhan's own order history (open item #3, exit-leg fill price).

---

## How to resume

Attach `TRACKING.md` + the latest repo zip in a new conversation.
Everything needed to continue is captured here — no re-derivation needed. Just say
which of the "Next steps" above to continue from, or describe what's changed.
