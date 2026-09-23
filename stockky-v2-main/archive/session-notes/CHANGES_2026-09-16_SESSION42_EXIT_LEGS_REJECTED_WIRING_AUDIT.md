# Session 42 — end-to-end re-audit: real-trade-service + position-stocks-service

Scope: full re-audit of both services for open/incomplete work, focused on
whether the `EXIT_LEGS_REJECTED` status introduced in session41b (super-order
exit legs both REJECTED/CANCELLED — circuit-limit or surveillance) was fully
wired everywhere the codebase already treats a position as "open" / still
holding real capital.

It was not. Three places still only checked `status="OPEN"`, all in
position-stocks-service:

## Bug 8 — `/status` `eod_stragglers` undercounts stuck positions (`main.py`)
The dashboard's own stragglers diagnostic — added specifically because this
service has no Telegram/notification channel and a failed EOD flatten had
"no operator-facing signal beyond a server log line nobody is watching" (see
its own comment) — only queried `status="OPEN"`. A position stuck as
`EXIT_LEGS_REJECTED` after the EOD sweep already ran (the exact scenario
session41b exists to make visible) was silently excluded from the one number
that exists to surface it.
**Fix:** query `status IN ("OPEN", "EXIT_LEGS_REJECTED")`.

## Bug 9 — Max-concurrent-positions gate undercounts real exposure (`orders/entry.py`)
`_count_open_positions()`, which gates `MAX_CONCURRENT_SCALP_POSITIONS`
before every new BUY, only counted `status="OPEN"`. An `EXIT_LEGS_REJECTED`
position still holds real capital and real market exposure (only its exit
legs failed, not its entry) but was invisible to this count — the service
could open more concurrent positions than the configured cap whenever a
stuck position existed.
**Fix:** same status tuple as above.

## Bug 10 — Capital ledger can double-spend reserved capital (`capital/ledger.py`)
`sync_from_broker()` decides whether it's safe to hard-reset
`available_capital` to the freshly-synced allocation by checking
`open_count == 0` — but `release_capital()` is never called when a position
transitions to `EXIT_LEGS_REJECTED` (only `status`/`error_message` change),
so its `capital_risked` stays reserved exactly like an `OPEN` position's
does. With the old `status="OPEN"`-only count, a stuck `EXIT_LEGS_REJECTED`
position could make `open_count` read 0 while capital was still actually
committed to it, causing `available_capital` to be reset to the full
allocation — effectively double-spending the capital already tied up in the
stuck position.
**Fix:** same status tuple as above.

## Verified NOT broken (checked, no change needed)
- `main.py`'s `open_syms` exclusion query (line ~332) and
  `orders/eod_squareoff.py`'s own sweep query already correctly include
  `EXIT_LEGS_REJECTED` — these were fixed in the session42 audit that
  introduced the pattern above; the three bugs above are the remaining spots
  that audit missed.
- `/positions` endpoint returns the most recent 50 positions by `opened_at`
  regardless of status, so `EXIT_LEGS_REJECTED` rows are already visible
  there.
- real-trade-service's own status filters (`OPEN`/`PARTIALLY_CLOSED`/
  `PENDING_EXIT`) were surveyed across `entry_engine/`, `exit_engine/`,
  `portfolio/portfolio.py`, `manual_engine.py`, `execution/auto_pilot.py`,
  and `main.py` — all consistently include the full status set; no
  equivalent gap found there. (real-trade-service does not have an
  `EXIT_LEGS_REJECTED`-equivalent status at all — its exit_engine's own
  circuit-limit path already notifies via `notifier.notify_critical`,
  see `exit_engine/exit.py` around the `circuit_limit_sell_alert_` snapshot
  key.)
- Session41b/42's earlier fixes (circuit-limit EOD fast-fail, both
  notifiers' dedup cache) were re-verified present and correct in this pass
  — not re-applied, already there.

All three fixes verified with `python3 -m py_compile` across every `.py`
file in position-stocks-service — no syntax errors introduced.

---

## Round 2 (same session, "audit more")

## Bug 11 — EOD flat-SELL capital released before fill is confirmed, never reclaimed on a dead order (`orders/eod_squareoff.py` + `orders/reconcile.py` + `capital/ledger.py`)
`eod_squareoff.py`'s `run_eod_squareoff()` calls
`ledger.release_capital(db, position_value=pos.capital_risked, realized_pnl=0.0)`
immediately after successfully **placing** the flat MARKET SELL — before
Dhan has confirmed any fill. That's intentional for the common case (so the
position shows CLOSED on the dashboard right away instead of waiting on the
next reconcile tick; the entry_price placeholder P&L is corrected later once
the real fill is known).

What wasn't handled: `orders/reconcile.py`'s `_reconcile_eod_pending()`
already detects the case where that SELL comes back `REJECTED`/`CANCELLED`
with **zero fill** (`_DEAD_EXIT_STATUSES` branch) — meaning the position is
still genuinely open at the broker, with real capital still at risk there.
It correctly marks the position `ERROR` and sends a critical alert, but
never told the ledger the earlier release was wrong. `available_capital`
stayed inflated by exactly `capital_risked`, silently overstating what's
actually free — able to fund a new position against capital that's really
still tied up in the zombie one at the broker.

**Fix:** added `ledger.reclaim_premature_release(db, capital_risked=...)`
(mirrors `reconcile_position_cost`'s delta mechanics, named for what it
actually undoes here) and call it from the `_DEAD_EXIT_STATUSES` branch
right after marking the position `ERROR`. Does not touch `realized_pnl` (no
trade happened) or the daily-loss kill switch (no loss booked) — purely a
correction to `available_capital`.

## Verified NOT broken in this round
- real-trade-service's own EOD square-off (`execution/auto_pilot.py`'s
  `_eod_squareoff`) does **not** have this problem — it never releases
  capital eagerly at all; `p.qty_open` and any capital/risk bookkeeping are
  only adjusted once `reconcile_real_orders()` confirms the real fill
  against Dhan's trade book (see that function's own comment). The
  premature-release pattern is specific to position-stocks-service's
  placeholder-first EOD design.
- Re-checked `_backfill_legacy_eod_exit_order_ids` and the broker
  orderType/price mismatch alerts in `_reconcile_eod_pending` — both sound,
  no change needed.

Fix verified with `python3 -m py_compile` across every `.py` file in both
services.
