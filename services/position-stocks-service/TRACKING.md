# Position Stocks — Project Tracking Document
**Purpose of this doc:** continuity anchor. If chat context/limits reset, attach this doc + the latest Stockky zip in a new conversation and work continues from exactly here — nothing re-derived from scratch.

**Last updated:** 2026-09-12 (session 4)
**Status:** All of §5 (steps 1–9) complete; two new items added and completed this
session (§5.10, §5.11 — master module enable/disable toggle, 1-minute screening
window). Still not yet smoke-tested against a live Dhan/Angel One session. User
wants to go straight to REAL money live testing (no DEMO/paper phase) once deployed —
see STATUS.md for the full up-to-date checklist and next steps; this file stays the
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
- **Migration note:** SQLAlchemy's `create_all()` only creates missing tables, never
  ALTERs existing ones. If `scalp_gate_state` already exists in the deployed DB
  (i.e. a prior boot got far enough to run `init_tables()` before crashing), it needs
  a manual `ALTER TABLE scalp_gate_state ADD service_enabled NUMBER(1) DEFAULT 1`
  (Oracle) before this deploys cleanly. Per §7's deploy log, no boot has gotten past
  `init_tables()` successfully yet, so this is very likely a non-issue — but check the
  table before redeploying, just in case.

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
    wired through screener + frontend. No open items left in §5.

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
