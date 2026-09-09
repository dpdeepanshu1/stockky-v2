# Review fixes — 2026-08-27

Based on a review of the Oracle VM deploy log (`docker compose up -d --build`
+ `docker compose logs -f`) and the full `services/real-trade-service` code.
All existing code/paths preserved — these are targeted edits, not a rewrite.

## 1. Telegram — already wired correctly (no fix needed)
Checked both notification paths end-to-end:
- `services/real-trade-service/notifier.py` — BUY/SELL sent, fills, auto-pilot
  cycle summaries, auto-pilot errors. Called from `entry_engine/entry.py`,
  `exit_engine/exit.py`, `execution/auto_pilot.py`, `execution/reconcile.py`.
- `services/notification-scheduler-service/notification/main.py` — scan/candidate
  opportunity alerts, `_send_telegram()`, wired into `/notify` and `/test`.

Both fail closed (never raise, never block an order) and both read
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`. Nothing to change here — just make
sure both env vars are actually set in `.env` on the VM (the log doesn't show
either service actually sending a message, which usually just means no
BUY/SELL/exit has happened yet, not that it's broken).

## 2. Dhan token lifetime was wrong — the actual root cause of the timing issue
`services/real-trade-service/config.py` already had the correct default
(`DHAN_TOKEN_LIFETIME_DAYS=1`, i.e. 24h — Dhan's access tokens are hard-capped
at 24 hours for every account, SEBI/exchange-mandated, confirmed against
Dhan's current docs/support pages). But **`docker-compose.yml`,
`.env.example`, and `.env.oracle.example` all overrode it to `30`** — so the
container was actually running with a 30-day countdown while Dhan kills the
token in 24h.

Effect of the bug: the gate never auto-disarmed on schedule. REAL orders
placed with an already-dead token would just fail one-by-one with an auth
error, get logged, and retry next cycle — while the dashboard kept showing
🟢 armed and the countdown kept claiming ~29 days left.

**Fixed**: default changed to `1` in all three files.

## 3. Added a real-time Dhan token check (not just the local clock)
New: `execution/dhan_client.py::verify_token_live()` /
`is_auth_error()`, and `auth/dhan_credentials.py::enforce_live_token()`.

The 24h local countdown is now correct, but it can't see a token Dhan kills
*early* — generating a new token on Dhan Web instantly invalidates the old
one regardless of the 24h clock, and there can be clock drift. The new
`enforce_live_token()` makes one cheap read-only Dhan call
(`get_fund_limits`) at the very start of every cycle (`cycle_runner.py`,
used by both the manual "Run Cycle" button and the Auto-Pilot background
loop) and immediately disarms + sends a Telegram alert if Dhan itself
rejects the token — before wasting a cycle trying to size/place orders that
would just fail. A generic/transient Dhan hiccup is *not* treated as a dead
token — only a recognized auth-rejection message trips it.

## 4. Fixed a false-warning flooding the logs
`dhan get_holdings failed: Dhan API error: No holdings available` was firing
on basically every poll. Dhan's own SDK reports "zero holdings" as
`{status: "failure", remarks: "No holdings available"}` — not an empty list —
which is completely normal for a small/fresh/all-cash account, not an actual
error. `execution/dhan_client.py::get_holdings()` (and `get_positions()` for
the equivalent "no positions" case) now treats that specific message as an
empty list instead of raising.

## 5. Risk engine didn't check actual cash — the "₹1000 split" bug
`risk_engine/engine.py` sized every order only against *risk %* of equity
(how much you can afford to **lose**), and never checked whether the account
had enough **cash** to actually **buy** that many shares. On a small account
this is the difference between "correct" and "overspends":

- Added check **4b (cash-available cap)**: down-sizes qty to whatever
  `cash_available` genuinely allows (never up-sizes), rejecting outright
  only if even 1 share doesn't fit.
- Added **reserved-cash tracking** across one cycle's candidate loop in
  `entry_engine/entry.py`: approving candidate #1 for e.g. ₹600 of a ₹1,000
  account now means candidate #2, evaluated moments later in the *same*
  cycle, sees only ₹400 available — not the stale full ₹1,000 — since a
  real fill/cash deduction doesn't land in the DB until later (DEMO fills
  in `check_pending_fills`, REAL fills only after broker reconciliation).
  Without this, two or three candidates in one cycle could each get
  independently approved against the same starting cash and collectively
  commit more than the account actually has.

## 6. Pinned scikit-learn to match the trained model
The pickled model (`data/prediction-model/model.pkl`) was trained on
scikit-learn 1.5.0; `requirements.txt` pinned 1.5.1, producing an
`InconsistentVersionWarning` on every boot (harmless today, but worth
removing rather than assuming two patch versions calibrate identically
forever). Pinned to `scikit-learn==1.5.0` in
`services/decision-prediction-service/requirements.txt`.

## Noted but NOT changed (by design / low value to touch)
- **`candidate fetch ... ReadTimeout`** (api-gateway `/stockky-hot`) — already
  a documented best-effort call with a generous 25s timeout; a slow cycle
  just retries candidates next tick. Could raise the timeout further or add
  a retry, but this trades off cycle latency for marginal benefit — leave
  as-is unless timeouts become frequent.
- **`Using enhanced static fallback list with 175 symbols`** — intentional
  fallback when NSE's own scrape endpoint fails/rate-limits (common from
  cloud IPs, including Oracle Cloud). Not a bug; would need a different
  (likely paid) data source to eliminate entirely.
- **`HF_API_KEY not set; using neutral fallback`** — intentional, optional
  key; news sentiment just runs neutral without it.
- **Root-level `config.py`/`main.py`/etc. (repo root, not under `services/`)**
  — this is a **stale, pre-Phase-3 duplicate** of `real-trade-service`: it's
  missing `auto_pilot.py`, `notifier.py`, `equity_sync.py`, `reconcile.py`,
  `cycle_runner.py` entirely, and its own `config.py` still has the old
  `DHAN_TOKEN_LIFETIME_DAYS` default of 30. `DEPLOY_GUIDE.md` deploys every
  other service from `services/<name>`, so this root copy isn't the live
  deployment target — but it's confusing to leave around. **Recommend
  deleting the root-level duplicates** (`config.py`, `main.py`, `db.py`,
  `models.py`, `auth/`, `execution/`, `entry_engine/`, `exit_engine/`,
  `risk_engine/`, `portfolio/`, `candidate_engine/`, `market_feed/`,
  `audit/`, `oracle_compat.py`, `tz_utils.py`) since `docker-compose.yml`
  never builds from the repo root for this service — only left in place
  this round in case something on Render still points at it.

## On the ₹1,000 / minimum-balance question
See the chat explanation for the full walkthrough and formula; short version:
with the seeded defaults (1% risk per trade, ~3.2% flat stop when no ATR is
available), the system needs roughly `entry_price × 3.2` in equity just to
afford **one share** of a stock at that price. A ₹1,000 balance can only
size 1+ shares of stocks priced under roughly ₹300, and will legitimately
`WAIT` (not error) on anything pricier — that's the risk cap doing its job,
not a bug. A more workable minimum for typical NSE mid-cap prices
(₹200–₹1,500) is roughly ₹5,000–₹10,000 at these default settings, or raise
`risk_per_trade_pct` (admin-editable while disarmed) if you intentionally
want to trade small.
