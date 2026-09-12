# Position Stocks — Project Tracking Document
**Purpose of this doc:** continuity anchor. If chat context/limits reset, attach this doc + the latest Stockky zip in a new conversation and work continues from exactly here — nothing re-derived from scratch.

**Last updated:** 2026-09-12 (session 2)
**Status:** Steps 1–8 of §5 implemented (backend fully built, including the exit-reconciliation
monitor added this session; frontend "Position Stocks" tab now built too). Only real
open item left: user must confirm `RISK_PER_TRADE_PCT` (§3.5/§5.9). Not yet deployed
or smoke-tested against a live Dhan/Angel One session. User wants to go straight to
REAL money live testing (no DEMO/paper phase) once deployed — see STATUS.md for the
full up-to-date checklist and next steps; this file stays the static architecture
record, STATUS.md is the living progress tracker.

---

## 1. Background — existing system (context, not the new work)

- **Stockky v2**: microservices platform for Indian stock scanning/analysis/auto-trading. Python FastAPI backend, React/TS frontend. Repo: `github.com/dpdeepanshu1/stockky-v2`. Deployed via docker-compose on an Oracle Cloud VM, directory `~/stockky-v2`.
- Services: `api-gateway`, `market-data-service`, `analysis-intelligence-service`, `decision-prediction-service`, `notification-scheduler-service`, `real-trade-service`, `frontend`.
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
- New standalone container: `position-stocks-service`. Own process, own event loop, own docker-compose entry.
- No runtime imports from `real-trade-service`. Dhan auth / security-id-cache / order-placement helpers are **duplicated** into this service's own module (copied logic, not shared code) — a bug or slowdown in one can never touch the other.
- Own `/health` endpoint, own circuit breaker, own daily-loss kill switch (tighter threshold than the core system, since scalping is higher variance).

### 3.2 Data feed — Angel One WebSocket (FREE)
- **User rejected Dhan's Data API (₹499/mo, confirmed via user's own screenshot of `web.dhan.co/index/profile` — flat subscription, not trade-count-waived as some third-party sites claimed).**
- Decision: use **Angel One SmartAPI WebSocket (`smartWebSocketV2`)** instead — genuinely free, no subscription.
- Reuse existing `AngelOneSession` login flow (already gets `feed_token`) and existing `angelone_scrip_master.py` symbol→token map.
- **New build required**: a true persistent WS client (binary frame parsing, subscribe/heartbeat/reconnect) to replace the fake "ws_feed" (currently just REST polling every 3s). This is the one genuinely new piece of low-level infrastructure in this project.
- Important structural note: data comes from **Angel One** (symbol → AngelOne token), but orders execute on **Dhan** (symbol → Dhan `security_id`). The service needs both maps side by side, keyed by the same clean symbol string, so a signal found via the Angel One feed resolves immediately to the right Dhan security for order placement.

### 3.3 Screening engine
- In-memory per-symbol ring buffer: (timestamp, LTP, volume). O(1) rolling %-change computation over trailing 5m/15m/1h per tick — no DB read on the hot path.
- Gates layered on top: min liquidity/avg volume, max bid-ask spread, exclude symbols already in an open scalp position, exclude illiquid/penny stocks.
- Runs **all three windows simultaneously** as independent candidate sources feeding one shared ranking step (composite score, similar spirit to `real-trade-service`'s existing Gate 6) — not "always prefer 5-min." Best candidate(s) win regardless of which window surfaced them.

### 3.4 Order execution — Dhan Super Order (bracket), FREE, chosen over manual OCO
- Confirmed: Super Order is part of Dhan's **Trading API**, which is unconditionally free (unlike the Data API).
- One call places entry + target + stoploss legs atomically — faster and safer than a manual 3-order OCO (no race-condition window between entry fill and exit legs going live).
- **Verified directly from the `dhanhq` Python SDK source (`_super_order.py`):**
  - `place_super_order(security_id, exchange_segment, transaction_type, quantity, order_type, product_type, price, targetPrice, stopLossPrice, trailingJump, tag)`.
  - `price` (entry reference) **must be > 0**, even when `order_type="MARKET"` — Dhan validates `targetPrice`/`stopLossPrice` against this reference (`targetPrice > price` and `stopLossPrice < price` for a BUY, reversed for SELL). Plan: pass the current Angel One LTP as this reference price at the moment of firing; actual fill still executes at market via `order_type="MARKET"`.
  - `product_type` must be **`"INTRA"`** — NOT `"CNC"` (which `real-trade-service` uses for its multi-day-hold entries). INTRA is required since scalp positions must close same day, and it uses less margin.
  - Order types confirmed available in the SDK: `LIMIT`, `MARKET`, `SL` (`STOP_LOSS`), `SLM` (`STOP_LOSS_MARKET`).
  - Modification support exists per-leg (`ENTRY_LEG` / `TARGET_LEG` / `STOP_LOSS_LEG`) via `modify_super_order()` — useful later for trailing-stop logic if wanted.
- **This is a genuinely new code path — never called anywhere in this codebase before.** User has chosen to go straight to real money without a DEMO/paper phase. Recommended (not mandatory) mitigation: fire the very first live Super Order at minimum quantity to observe Dhan's actual response/behavior before trusting it at normal position size.

### 3.5 Capital sizing — adaptive, risk-based
- Not a flat rupee amount per trade. Formula:
  `position_value = (fixed % of the scalp pool risked per trade) ÷ (that stock's own adaptive stoploss %)`
- A volatile stock with a wider adaptive stop automatically gets a smaller position; a calmer stock gets a bigger one — rupee-at-risk stays roughly constant per trade.
- **OPEN ITEM — still needed from user:** what % of the scalp pool should be risked per single trade (e.g. 1%, 2%)? Not needed to finalize architecture, but needed before the sizing formula has real numbers.

### 3.6 Capital split
- 50/50 with `real-trade-service`, enforced entirely in software via a new `ScalpCapitalLedger` table — **Dhan itself does not segregate a single account's funds into pools.** Both services independently check their own ledger's remaining allowance before sizing any order, cross-checked against Dhan's live fund-limit API so neither can overspend into the other's half.

### 3.7 Position limits & hold time
- Max **5** concurrent scalp positions. Min **1** preferred but never forced onto a bad/absent signal.
- No fixed hold timer — adaptive target/SL, exits the instant either triggers (via Super Order's own bracket legs).
- Hard **3:00 PM IST** square-off sweep force-closes anything still open, MARKET order, no exceptions (same shape as `real-trade-service`'s own `EOD_SQUAREOFF_TIME_IST`, currently 15:00, but this cutoff is independent and specific to this pool).

### 3.8 Shared broker-side constraint
- Dhan's account-wide order cap (~5,000–7,000 orders/day per Dhan support docs) is **shared** between `real-trade-service` and this new service — same account. Plan: a shared Redis counter incremented by both services on every order call, with this service given its own conservative sub-ceiling, so a busy scalp day can never starve `real-trade-service`'s ability to place its own orders.

### 3.9 Dashboard
- New frontend tab: **"Position Stocks."**
- Live screener grouped by window (5m/15m/1h), open scalp positions with live P&L, today's pool P&L, remaining scalp capital, manual kill switch.

### 3.10 Rollout
- **User's explicit choice: go straight to REAL money, live market, no DEMO/paper phase** — based on weeks of hands-on trust with `real-trade-service`. Noted once as a risk (new untested order-type path) above; proceeding per user's decision.

---

## 4. New DB objects planned (not yet created)
- `ScalpPosition` — open/closed scalp trade records, separate from `real-trade-service`'s `TradePosition` table.
- `ScalpCapitalLedger` — this pool's allocated/available cash, independent of the core system's ledger.
- `ScalpCandidateLog` — audit trail of scanned candidates and why each was taken/skipped (mirrors the diagnostic value of `real-trade-service`'s WAIT-reason logging).
- All writes async/non-blocking — DB slowness may delay logging, never a trade decision.

---

## 5. Implementation steps — status (see STATUS.md for full detail on each)
1. ✅ Scope `position-stocks-service` folder structure + docker-compose entry.
2. ✅ Build the Angel One true-WS client (replacing the fake polling one, reusing existing session/token-map code).
3. ✅ Build the Dhan symbol→security_id side-map reuse (duplicate the relevant piece of `dhan_client.py`'s cache logic, not import it).
4. ✅ Build the rolling-window screening engine + composite ranking.
5. ✅ Build the Super Order integration (entry + target + SL), sandboxed/minimum-qty first live test.
6. ✅ Build `ScalpCapitalLedger` + the 50/50 enforcement logic.
7. ✅ Build the 3:00 PM square-off sweep. Shared Redis order-count guard still deferred (see STATUS.md open items — low urgency).
   Also added, unplanned but necessary: the Super Order exit reconciliation monitor (`orders/reconcile.py`) — polls
   Dhan's super order book for TARGET_LEG/STOP_LOSS_LEG fills and closes positions + releases capital automatically.
8. ✅ Build the "Position Stocks" frontend tab (`positionStocksApi.ts` + `PositionStocksTab.tsx`, wired into `App.tsx`).
9. ⏳ OPEN — get the risk-per-trade % from user to finalize capital sizing formula (§3.5). Placeholder 1.5% active,
   surfaced as a warning both in the startup log and the frontend tab.

---

## 6. How to resume this if context resets
Attach this document **plus the latest Stockky repo zip** in a new conversation. Everything needed to continue — decisions made, code already verified (file/line references above), and exactly what's still open — is captured here. No need to re-explain the plan or re-derive the architecture; just say which of the numbered steps in §5 to continue from.

**As of session 2, `services/position-stocks-service/STATUS.md` inside the zip is the
live, more-detailed progress tracker** (file-by-file, with exact open items and a
prioritized next-steps list) — read that first, this document second for the
original architecture rationale. Both travel together in every zip from now on.
