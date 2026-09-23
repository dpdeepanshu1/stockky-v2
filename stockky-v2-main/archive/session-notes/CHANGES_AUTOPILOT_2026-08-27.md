# Stockky — Manual Buy fix + Auto-Pilot (2026-08-27)

## The actual bug (why there was "no buy button")

`ManualTradeTicket.tsx` was never broken — Review → Confirm BUY only
appears when the backend's risk check returns `ok: true`. It never did
for REAL mode, because `trade_accounts.REAL` is seeded at
`current_equity = 0.0` / `cash_available = 0.0` on first boot (see
`main.py: _seed_defaults`) and **nothing ever updated it from your real
Dhan balance**. The risk engine's per-trade-risk-cap check computes
`max_trade_risk = equity * risk_per_trade_pct`, so with equity stuck at
₹0, every single BUY — manual or automatic — was rejected before a
Confirm button could ever render. It was never really about having a
"limited balance"; the service's own ledger thought the balance was
zero.

Same root cause is why an armed "Run Cycle" never actually entered a
position for REAL: `entry_engine` sizes qty the same way.

## Files changed

- **`services/real-trade-service/execution/equity_sync.py`** (new) —
  pulls your live available balance from Dhan (`get_fund_limits`) and
  refreshes `cash_available`/`current_equity` before every risk
  decision. This is the actual fix.
- **`entry_engine/entry.py`**, **`manual_engine.py`** — call the sync
  above at the top of their account-state builders (REAL only).
- **`cycle_runner.py`** (new) — the `/cycle/run/{mode}` logic extracted
  into one function so both the manual button and Auto-Pilot use the
  exact same code path.
- **`execution/auto_pilot.py`** (new) — background asyncio loop, started
  at app boot, that runs `cycle_runner.run_cycle_core` every
  `AUTO_PILOT_INTERVAL_SECONDS` (default 180s) for any mode that is both
  **armed** and has **Auto-Pilot turned on**, only during NSE market
  hours (Mon–Fri 09:15–15:30 IST). This is what makes buy/sell keep
  happening with the browser closed.
- **`notifier.py`** (new) — direct Telegram sender (`TELEGRAM_BOT_TOKEN`
  / `TELEGRAM_CHAT_ID`), used by Auto-Pilot, order rejections, and every
  broker-confirmed fill/exit (`execution/reconcile.py`,
  `exit_engine/exit.py`).
- **`models.py`**, **`db.py`** — added `auto_pilot_enabled` /
  `auto_pilot_enabled_at` to `trade_gate_state`, with the same additive
  migration pattern already used for `trade_orders`, so it's safe to
  deploy on top of an existing database.
- **`main.py`** — new `POST /autopilot/{mode}/enable` /
  `/autopilot/{mode}/disable` routes; `/status/{mode}` now returns
  `auto_pilot_enabled`; starts the Auto-Pilot loop at boot (inert unless
  a mode is both armed and toggled on).
- **`tz_utils.py`** — added `is_market_open_ist()`, now used instead of
  the old hardcoded `market_is_open=True` in `entry_engine`.
- **`frontend/src/realTradeApi.ts`**, **`RealAutoTrade.tsx`** — Auto-Pilot
  ON/OFF toggle next to the "Run Cycle" button (Pipeline tab).
- **`.env.example`**, **`docker-compose.yml`** — documented
  `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` (shared with the existing
  scan-notification service), `AUTO_PILOT_INTERVAL_SECONDS`,
  `AUTO_PILOT_NOTIFY_HEARTBEAT`.

## What you need to do after deploying this

1. **Redeploy `real-trade-service`** (this is the only service that
   changed). The new `trade_gate_state` columns are added automatically
   on boot — no manual SQL needed.
2. **Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`** in that service's
   environment (same values work if you already use them for the scan
   notifications) — message @BotFather to create a bot, message it once,
   then hit `https://api.telegram.org/bot<token>/getUpdates` to read your
   chat_id.
3. In the dashboard: **Login → Connect Dhan → confirm Risk Config → Arm
   REAL.** Once armed, open the **Pipeline tab** — you should now see
   real numbers instead of ₹0 under the account/equity display, because
   `/status/REAL` now syncs from Dhan.
4. Try a **manual BUY** again: Review should now come back approved for
   whatever quantity your actual balance and risk % support, and the
   CONFIRM BUY button will appear.
5. Flip **Auto-Pilot → Turn On**. From then on, during market hours, the
   server runs the same cycle every 3 minutes on its own and Telegram
   tells you what happened — no need to keep the site open.

## Important, please read

- With a **very small account**, the 1% default `risk_per_trade_pct`
  may mean many candidates size down to 0 shares and get skipped — that
  is the risk engine working as intended, not a bug. You can raise
  `risk_per_trade_pct` in Risk Config (while disarmed) if you
  deliberately want to risk a larger slice of a small account per
  trade, but that also means bigger drawdowns on a losing trade.
- Auto-Pilot only runs while the `real-trade-service` process itself is
  up. If it's hosted on a platform that sleeps idle services, it won't
  tick while asleep — keep it on a plan/host that stays awake if you
  need it running unattended all day.
- This is real money once REAL is armed. Start with Auto-Pilot on DEMO
  first to watch a few cycles end-to-end before trusting it on REAL.
