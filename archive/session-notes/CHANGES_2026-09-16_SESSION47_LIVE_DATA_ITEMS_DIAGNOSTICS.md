# Session 47 — closing what CAN be closed on the "needs live data" list without guessing

Went back through the four items flagged as "genuinely need live data, not
guessed at" (DATAMATICS retry theory, emergency_gap_down retry pattern,
exit-leg fill-price field guess, STATUS.md live-only items) to separate
what's actually still blocked on live data from what could be resolved by
just reading the code more carefully or adding better instrumentation.
Result: one item's uncertainty was resolved by code inspection alone (no
live data needed), and two items got additive diagnostics that will
confirm/refute themselves in the logs the next time they occur live,
instead of requiring a manual eyeball comparison against Dhan's app.

## Resolved by code inspection — no live data needed

**`emergency_gap_down` retry pattern (real-trade-service, item #7):**
traced every call site. `exit_engine/exit.py`'s emergency-gap-down branch
calls `_send_real_sell(db, position, position.qty_open, "emergency_gap_down")`
— the exact same function every other automatic exit reason
(`stop_hit`/`target_hit_partial`/`time_stop`/`eod_squareoff`) calls, and the
`consecutive_exit_failures` cooldown check (session40's fix) sits at the
very top of that function, before any reason-specific branching. There is
no separate or bypassing retry path for `emergency_gap_down` specifically
— it is backed off by the exact same exponential cooldown
(`EXIT_RETRY_BASE_COOLDOWN_SECONDS` doubling per consecutive failure,
capped at `EXIT_RETRY_MAX_COOLDOWN_SECONDS`) and covered by the same
`EXIT_RETRY_ALERT_THRESHOLD` operator alert as any other reason. This
closes the *structural* question session 39/43 left open ("is this
retrying unboundedly or is it governed") — it is governed, by construction,
confirmed by reading the actual call graph, not by observing live
behavior. What genuinely still needs live data is only whether the
*tuning* (the base cooldown / threshold values) feels right in practice —
that's a live-observation question, not a code-correctness one.

## Additive diagnostics — will self-resolve the next time they fire live

**DATAMATICS SDK MARKET→LIMIT theory + retry-cause question
(real-trade-service, items #5/#7's remaining tuning question):**
`execution/reconcile.py` now captures Dhan's own rejection reason on any
dead (REJECTED/CANCELLED) SELL — `omsErrorCode`/`omsErrorDescription`,
confirmed as real fields on Dhan's documented order-object schema
(https://dhanhq.co/docs/v2/postback/; not independently reconfirmed that
GET /orders' orderbook response echoes them identically to the Postback
payload, but the extraction uses the same graceful multi-key `_get()`
fallback this module already uses everywhere — if absent, it's just
`None`, exactly like today). Threaded through to:
- the `TradeOrderEvent` detail line for the dead order (so it's visible
  in the trade history/order-ids UI, not just logs),
- `_track_exit_failure_and_maybe_alert()`'s operator Telegram alert and
  its `logger.critical(...)` line, once the streak crosses
  `EXIT_RETRY_ALERT_THRESHOLD`.
Purely additive — every existing field/behavior is unchanged;
`rejection_reason`/`rejection_code` default to `None` and the alert text
just gains an extra line when they're present. The next time a rejection
storm happens, the alert itself will say *why* Dhan rejected it instead of
just *how many times* — enough, on its own, to confirm or kill the SDK
MARKET→LIMIT theory without needing a manual Dhan-order-book cross-check.

**Exit-leg fill-price field guess (position-stocks-service, item #8):**
`orders/reconcile.py`'s `_extract_leg_price()` already had a sound
fallback chain (several plausible key names → leg's own trigger price →
this position's own known target/stop) but was silent about *which*
branch actually resolved. Added one `logger.info(...)` line per branch:
when a real `averageTradedPrice`-style key resolves, it logs which key and
the value; when it instead falls through to the leg's static `price`
field, it logs that explicitly (and that this refutes the
`averageTradedPrice`-on-leg assumption). No return-value or fallback-order
change — same prices, same behavior, just visible in the logs which
branch fired the first time a real TARGET_LEG/STOP_LOSS_LEG exit happens,
closing this item from a "go eyeball Dhan's app" task down to "read the
next exit's log line."

## Still genuinely blocked on live data

- **STATUS.md's "Next steps" live-only items** (first live Super Order
  test, EOD-disarm flatten test, live timestamp check, circuit-breaker-open
  smoke test) — all require your live VM/market hours; nothing in the code
  can substitute for actually observing these.
- **DATAMATICS SDK MARKET→LIMIT theory itself** — the diagnostic above will
  surface the evidence the next time it happens, but the theory can't be
  confirmed or refuted from the sandbox before that.
- **Exit-retry cooldown/threshold *tuning*** (as opposed to the now-settled
  structural question above) — whether `EXIT_RETRY_BASE_COOLDOWN_SECONDS`/
  `EXIT_RETRY_ALERT_THRESHOLD`'s current defaults feel right in practice is
  a live-observation call.

## Verification
- `python3 -m py_compile` clean on both touched files
  (`real-trade-service/execution/reconcile.py`,
  `position-stocks-service/orders/reconcile.py`).
- `pyflakes` clean on both (no new findings).
- Confirmed `_track_exit_failure_and_maybe_alert()` has exactly one call
  site (already updated to pass the new kwargs) and no other caller needs
  updating.
- No DB schema change — `rejection_reason`/`rejection_code` are
  request/log-scoped only, not persisted columns, so this carries zero
  migration risk.
