# position-stocks-service — full wiring/mapping audit (2026-09-13)

Full read-through of every file in `services/position-stocks-service/`
(main.py, config.py, models.py, db.py, and every module under auth/,
capital/, execution/, feed/, orders/, resilience/, screening/) cross-
checking config attributes against usage, model columns against DB
migrations, and every documented invariant ("no exceptions" exit rule,
"every timestamp must go through iso_utc()") against what the code
actually does. All findings below are confirmed by reading the code, not
inferred — no live testing was possible outside market hours.

## Fixed this session

1. **`db.py` — missing column migrations for `scalp_gate_state`.**
   `daily_loss_kill_switch_tripped`, `daily_loss_kill_switch_tripped_date`,
   `orders_placed_today`, `orders_placed_today_date`, and
   `first_live_order_done` are all declared on the `ScalpGateState` model
   and actively read/written by `main.py` and `orders/entry.py` (order-
   budget guard, first-live-order safety valve, kill-switch checks) — but
   none of the five had a `_COLUMN_MIGRATIONS` entry. Any `scalp_gate_state`
   table created before these fields were added to the model is missing
   the actual DB columns, so every query touching a `ScalpGateState` row
   (i.e. almost every request this service handles) would fail at the DB
   level. Same bug class already fixed for `scalp_capital_ledger` in an
   earlier session — that fix only covered one of the two tables that
   needed it. Added all five tuples.

2. **`orders/eod_squareoff.py` — EOD flatten-all-positions sweep could be
   silently blocked by the arm switch.** The mandatory closing
   `dhan_client.place_order(...)` call passed `is_armed=gate.is_armed`.
   `place_order()` raises `DhanNotArmedError` whenever `is_armed=False`,
   with no exemption for SELL/exit orders — unlike real-trade-service's
   risk engine and manual_engine.py, which explicitly exempt exits from
   the armed gate. A disarmed service (after `/disarm`, `/kill`, or simply
   not yet re-armed that morning) with open positions would hit this exact
   sweep, fail every closing SELL with `DhanNotArmedError` (caught, logged,
   position left OPEN), and leave real-money positions unflattened past
   3pm — exactly the scenario the tracking doc's §3.7 "no exceptions" rule
   for exits exists to prevent. Fixed by forcing `is_armed=True` for this
   specific mandatory-exit call, matching how `cancel_order`/
   `cancel_super_order` are already unconditionally allowed regardless of
   arm state.

3. **`main.py` / `capital/ledger.py` — every timestamp in every API
   response was raw, never passed through `iso_utc()`.** `tz_utils.py`'s
   own docstring says every JSON timestamp this service sends to a
   frontend MUST go through `iso_utc()` — a DB-sourced datetime comes back
   naive even though it was written as UTC, so a bare `.isoformat()`
   prints no offset, and a browser then parses it as local time instead of
   UTC (off by exactly +5:30, the IST offset). `real-trade-service/main.py`
   applies `iso_utc()` consistently across every timestamp field it
   returns; `position-stocks-service/main.py` duplicated `tz_utils.py`
   verbatim (including this exact docstring) but never actually called
   `iso_utc()` anywhere. Fixed: `armed_at`, `last_cycle_run_at` (in
   `/status`), `opened_at`/`closed_at` (in `/positions` and
   `/trades/history`), `created_at` (in `/candidates/log`), and
   `last_synced_from_broker_at` (in `/ledger`) now all route through
   `iso_utc()`.

4. **`capital/ledger.py` — `reset_daily()` was unreachable.** The
   function's own docstring describes it as "an admin route for testing or
   an emergency override," but no route ever called it — the automatic
   lazy reset-on-date-change covers the normal midnight case, but the
   manual/emergency path the function was written for didn't exist. Added
   `POST /ledger/reset-daily` (admin-gated) in `main.py`.

## Flagged, not changed

- **`config.MIN_PREFERRED_SCALP_POSITIONS`** is declared, defaults to 1,
  and is documented in the surrounding comment block as a real knob — but
  is never read by `screening/engine.py`, `orders/entry.py`, or anywhere
  else in the service. Whatever behavior this was meant to drive was never
  implemented. Left as-is with a comment flagging the gap rather than
  guessing at the intended semantics and inventing new trading logic —
  that's not a safe call to make without knowing what was originally
  planned. If this is meant to do something (e.g. relax thresholds to try
  to keep at least N positions open), it needs to be designed, not
  patched.
- **`config.MAX_SPREAD_PCT`** is also unused, but this one IS already
  explained in `screening/engine.py`'s own docstring: the spread gate is
  deliberately deferred until real bid/ask data is available (WS mode 1 is
  LTP-only). Not a bug — an intentional, documented deferral.
- **`feed/scrip_master.py`'s `_load_sync()`** makes a blocking
  `httpx.get()` call and is invoked from inside the async `_ws_loop()` via
  `get_all_nse_eq()` with no `await`/thread-offload. This blocks the event
  loop for the duration of that HTTP call (once per `REFRESH_INTERVAL_S`,
  24h, or on first load). Noted but not changed this session — didn't want
  to touch startup/reconnect timing without being able to test it live
  against a real WS connection.

## Not re-verified live

Everything above was confirmed by reading the code. The `scalp_gate_state`
migration gap (finding #1) can only be fully confirmed against a specific
deployed database — if that table was created fresh (after all five
columns already existed in models.py), the missing migrations are latent
rather than currently biting. Recommend checking on the next boot: the
new migrations log a `WARNING` line ("migrated — added missing column...")
if they actually do anything; silence means the columns were already
there.
