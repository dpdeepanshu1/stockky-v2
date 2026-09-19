# Session 72 — open-issues sweep (2026-09-19)

Scope: the 12 open items from the v7 hand-off. Every item below says what was **changed in code**, what is
**tested offline**, and what still **needs a live check** (nothing here was run against a real Dhan account).

| # | Item | Result |
|---|------|--------|
| 1 | Overnight-stop partial fills | 5 real bugs fixed + 13 offline tests. Live: still unverified against a real partial. |
| 2 | api-gateway / real-trade double restart | Probable cause found (500 MB cgroup limit) + forensics added. Root cause NOT proven. |
| 3 | `/scan/universe`, `/surprise/scan` under 25 s | 21 event-loop-blocking calls threaded + hard deadlines with stale fallback. Live: unverified. |
| 4 | DATAMATICS retry storm / `emergency_gap_down` | Real gap fixed (placement failures never fed the backoff) + 3 tests. |
| 5 | RML `error_message` sentinel | 3 stuck-sentinel paths closed + `/reconcile/pending` diagnostic. RML's actual row not inspected. |
| 6 | `MIN_PREFERRED_SCALP_POSITIONS` | Already wired (session 46). No change. |
| 7 | `MIN_FUNDAMENTAL_SCORE` floor (40) | Deliberately unchanged (real-money threshold, no data to justify a value). |
| 8 | Capital-starved retries | Per-symbol cooldown added (+ test). |
| 9 | `/dhan/funds` admin auth | No code defect reproduced; parity route + diagnostics + cross-service auth test added. |
| 10 | decision-prediction unread files | evaluate.py / trades.py: 4 real bugs fixed (+12 tests). app.py: 3 blocking calls threaded. models.py: pattern-swept only. |
| 11 | Frontend audit | `tsc --noEmit` strict = clean; pattern sweeps; 1 listener leak fixed. NOT a line-by-line read. |
| 12 | api-gateway `main.py` | AST sweeps (blocking-in-async, duplicate defs/routes, SQL formatting, HTTP timeouts). NOT a line-by-line read. |

## 1. Overnight-stop partial fills (position-stocks-service)
New `orders/overnight_stop.py` is now the single home of stop accounting. Bugs fixed:
1. **Re-arm never reset the fill counter** → after partial + re-arm the new order's cumulative qty was compared with the old
   order's booked qty (negative/short delta; position never closed). `assign_stop_order()` resets per-order counters and rolls
   them into `overnight_stop_prior_qty`.
2. **CANCELLED/EXPIRED orders with a partial fill were skipped**, and Dhan's order book is today-only. Now booked from the row,
   or from Dhan **trade history** (`dhan_client.get_trade_history`) when the order aged out (morning recheck).
3. **Deltas priced at the cumulative average** → priced at the chunk's own average via `overnight_stop_filled_notional_so_far`.
4. **`_fire_flat_sell` could oversell**: it cancelled the stop and sold full `pos.quantity` without booking a partial. Now settles
   before AND after the cancel; raises `PositionAlreadyFlat` if the stop already sold everything. `_resolve_pending_with_price`
   no longer overwrites earlier partial P&L.
5. Morning recheck looked for `PARTIALLY_TRADED`; Dhan's status is `PART_TRADED`. One shared vocabulary now.
Also: "TRADED but shares remain" is never closed silently — position stays OPEN, critical alert, stop id cleared.
Schema (additive, auto-migrated): `overnight_stop_filled_notional_so_far`, `overnight_stop_prior_qty`.
Tests: `tests/test_overnight_stop.py`. **Live check:** on the next carried position that partially fills, watch for
`overnight stop: … PARTIAL fill` log lines / the 🟠 Telegram, then `GET /positions` (`quantity`, `realized_pnl`).
Not handled (design): if the stock has gapped *below* the stop trigger by the morning re-arm, Dhan rejects a SL-M whose trigger
is above LTP → you get the "RE-ARM FAILED — manage manually" alert.

## 2. Double restart
Evidence: `api-gateway` had `mem_limit: 500m` for a 26 k-line service that runs pandas/yfinance scans; a cgroup OOM-kill is a
silent SIGKILL — exactly "restart with no crash/SIGTERM in the logs". Changes: limit raised to 900m (host budget in the compose
footer still holds); `boot_forensics.py` (identical copy in api-gateway, real-trade-service, position-stocks-service) logs one
`BOOT FORENSICS … cause=` line per boot: `FRESH_CONTAINER` / `RESTART_AFTER_CLEAN_SHUTDOWN` / `SIGTERM_BUT_NOT_CLEAN` /
`DIED_WITHOUT_CLEAN_SHUTDOWN` (= SIGKILL/OOM), plus `MEMORY PRESSURE` warnings at ≥85 % of the limit.
**Live check:** `docker inspect -f '{{.State.OOMKilled}} {{.RestartCount}}' api-gateway real-trade-service` and
`docker logs <svc> | grep -E "BOOT FORENSICS|MEMORY PRESSURE"`. A `docker compose up` that recreates dependents will also
show `FRESH_CONTAINER` — that would mean the restarts were deploy-induced, not crashes.

## 3. Scan latency
Sweep found **21** un-threaded blocking calls inside `async def` handlers in api-gateway `main.py` (sync `httpx.get` up to 25 s,
`yf.Ticker`, `_build_scan_universe`, `_get_momentum_movers`…) — each froze the whole gateway (incl. `/health`) while it ran.
All threaded. `/scan/universe`: movers under `SCAN_UNIVERSE_MOVERS_DEADLINE_S` (12), full build under
`SCAN_UNIVERSE_BUILD_DEADLINE_S` (20) with cached/stale fallback. `/surprise/scan`: `SURPRISE_SCAN_DEADLINE_S` (20) → last
result flagged `stale`/`deadline_exceeded`. Same class fixed in `training/app.py` (`stock_history`), `real-trade-service/
notifier.py` (`notify_async`), `analysis-intelligence-service/sentiment/main.py`. Responses gained additive flags
`momentum_movers_partial`, `deadline_exceeded`. **Live check:** call both endpoints with `?cached=true` right after a redeploy and
time them; grep logs for `not ready within` / `exceeded`.

## 4. Retry storm
`_send_real_sell` already had a 60 s→900 s doubling backoff keyed on `consecutive_exit_failures`, but only reconcile's
dead-order handler incremented it. A SELL failing at **placement** (SDK/API exception: CDSL, funds, IP, exchange-not-allowed,
generic) never did → retried every 45 s cycle for as long as it lasted (alerts merely throttled). Placement failures now feed the
same streak (generic errors only after `EXIT_REJECT_STREAK_ESCALATE_AT`; oversell and intraday-cutoff excluded).
`tests/test_exit_placement_backoff.py` fails on the old code, passes now. Trade-off: a persistent stop-loss SELL failure now
retries at most every `EXIT_RETRY_MAX_COOLDOWN_SECONDS` (900) instead of 45 s — lower it if you prefer.

## 5. Stuck `*_PENDING_RECONCILE` (RML)
Closed three paths: (a) a real TARGET/STOP close never cleared a stale earlier-EOD `error_message`; (b) prior-day rows can never
resolve from the today-only order list → now resolved from Dhan trade history (only when traded qty covers the position; a lone
matching SELL is adopted when `dhan_exit_order_id` is missing), booked to the ledger **total only** (never today's daily-loss
counter); (c) after `PENDING_RECONCILE_MAX_AGE_DAYS` (3) unresolved → rewritten `*_UNRESOLVED` + one alert. New:
`GET /reconcile/pending`, `POST /reconcile/pending/resolve` (admin). **Live check for RML:** `GET /reconcile/pending` — empty
list = resolved; otherwise the row shows its age and how it can resolve.

## 6 / 7. Config
#6 is already wired (`main.py`, `screening/engine.py`, `config.py`). #7 left at 40 — pick a value from live
`scalp_candidate_log` outcomes, not from code.

## 8. Cooldown
`CAPITAL_STARVED_COOLDOWN_S` (120) / `CAPITAL_STARVED_RETRY_ON_GROWTH_PCT` (25): a symbol skipped for capital is dropped before the
top-N slice (no quality-gate HTTP calls, no log rows) until the cooldown ends or available capital grows.

## 9. `/dhan/funds`
Login → token → admin route works in both services, and a token from one is accepted by the other (tested in subprocesses).
A `SESSION_SECRET` mismatch or a `$`-mangled hash is the realistic cause of "admin session broken". Added:
`GET /auth/config-check` (both services: SHA-256 fingerprint of the secret, hash-format flag), startup `AUTH CONFIG` log lines, and
`GET /dhan/funds` on position-stocks-service (admin-gated; Dhan errors → 409/502, never 401, because the dashboard clears the
session on any 401). **Live check:** compare `session_secret_fingerprint` from both `/auth/config-check` responses.

## 10. decision-prediction-service
`evaluate.py`: (a) **history_backfill labelled predictions with the last two bars of history — a move at/before the prediction
day** (label leak into `t1_success` → training). Off by default (`EVAL_ALLOW_HISTORY_BACKFILL`), not-yet-due predictions are no
longer queued; (b) T+5 scored **DO NOT BUY as a win when the stock rose**; (c) missing prices became 0.0 → false stop-loss /
−100 % adverse excursion. `trades.py`: weekly profit-take required `days_held % 7 == 0` (one missed sweep skipped the week).
Behaviour change: fewer T+1 labels until real next sessions exist. Existing rows already labelled by the backfill are NOT re-labelled.

## 11 / 12. Coverage statement (read this)
Frontend (≈23.8 k lines / 40 files): strict `tsc --noEmit` clean; sweeps for interval/listener/WebSocket leaks, `useEffect(async)`,
`||`/`&&` precedence, `dangerouslySetInnerHTML`/`eval`; flagged sites read. One fix (keep-alive listener). **Not read line-by-line.**
api-gateway `main.py` (≈11.8 k lines): duplicate defs (`wake_all_services` shadowing fixed), duplicate routes (none), blocking-in-
async (21 fixed), HTTP calls without timeout (0 repo-wide), SQL built by string formatting (65 sites triaged: table names /
dialect fragments / bound `:kN` placeholders only — no user input reaches SQL text). **Not read line-by-line.**
decision-prediction `models.py`/`app.py`: pattern sweeps only (+ app.py blocking calls fixed); `evaluate.py`/`trades.py` read in full.

## Tests
`cd services/position-stocks-service && python -m pytest tests -q` (16) ·
`cd services/real-trade-service && python -m pytest tests/test_exit_placement_backoff.py -q` (3) ·
`cd services/decision-prediction-service/training && python -m pytest tests -q` (12).
