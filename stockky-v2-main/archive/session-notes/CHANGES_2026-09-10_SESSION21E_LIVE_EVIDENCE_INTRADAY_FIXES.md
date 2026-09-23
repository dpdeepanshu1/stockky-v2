# Session 21e — real-trade-service audit, live-evidence-driven (2026-09-10)

Follow-up to session 21d. This round started from the actual Dhan order
book (screenshots of the live account: Orders tab showing **Success 12 /
Failed 205 / Cancel 1**, plus 9 individual rejected-order detail screens),
not just code reading — evidence of what's actually failing in
production, not just what looks risky on paper.

## What the rejected-order screenshots showed

Nine distinct rejections, clustered around 15:17-15:18 IST (just after
EOD_SQUAREOFF_TIME_IST=15:15 fires):

- 4x "Intraday orders cannot be placed at this time" (Medi Caps x3,
  Mrs. Bectors Food) — Dhan/NSE's end-of-day cutoff for fresh INTRADAY
  orders.
- 1x "Order rejected as this stock is not allowed to be traded in
  Intraday" (Medi Caps) — a different, security-level restriction.
- 2x "EXCH:16387: Security is not allowed to trade in this market"
  (Adani Power, Hyundai Motor India) — matches the already-fixed
  same-day-CDSL-settlement case from session 21c/earlier.
- 1x "insufficient funds. Please add Rs.54.89" on a SELL (Elgi Rubber
  Company) — matches the already-fixed broker_imported same-day case.
- 1x "Rate Not Within Ckt Limit 23.73 To 35.59" (Graviss Hospitality, a
  BUY at 35.70 — 0.11 above the upper circuit).

## Fix 1 — exit engine was hammering Dhan with doomed resends after cutoff

`exit_engine.exit._send_real_sell`'s intraday-cutoff branch (added
2026-09-08) already knew a cutoff rejection "can never succeed" again
today — but it only throttled the *Telegram alert* about it, not the
*resend itself*. With EOD_SQUAREOFF_TIME_IST=15:15 and the fast-exit loop
running every 45s (EXIT_CHECK_INTERVAL_SECONDS), a handful of positions
still open when the cutoff hits get re-sent — and re-rejected — every
cycle for the ~10-15 minutes left before close. That's easily 10-20
guaranteed-fail orders per stuck position in one session, which is very
plausibly most of the 205-failed-orders figure. Added a per-position,
per-IST-day suppression flag (reusing the existing snapshot-cache idiom):
once a cutoff rejection is seen, every later call this same day returns
`False` immediately without contacting Dhan at all. The position stays
open and gets retried fresh tomorrow, unchanged from the existing
next-day CNC behavior.

## Fix 2 — no detector at all for "stock can't trade Intraday" (permanent, per-security)

This is the more serious of the two: "not allowed to be traded in
Intraday" is a **permanent, per-security** restriction (trade-to-trade /
ASM / GSM surveillance stocks can never use `product_type="INTRADAY"`,
any time of day) — completely different from the time-of-day cutoff
above, and previously had **zero detection**. It fell into the generic
catch-all branch: retried every cycle, streak-escalated as if it might
eventually succeed, no indication of the real cause.

The real risk this created: for a security under this restriction, a
**same-day stop-loss or target hit was completely unexitable that day** —
the CNC sell fails (CDSL hasn't settled today's buy yet) *and* the
INTRADAY sell fails (this restriction) — with no fallback. The position's
stop-loss protection silently did nothing until it aged past "same-day"
and a CNC sell became viable the next day. Added
`dhan_client.is_security_intraday_restricted_error` and a matching branch
in `_send_real_sell`: recognized, doesn't count toward the reject-streak
escalation, one clear throttled alert explaining the actual mechanism
(not a generic "N consecutive rejections"), and reuses Fix 1's
per-position suppression so it isn't resent for the rest of the day
either.

## Reviewed but not changed — lower confidence, likely already covered or one-off

- **"insufficient funds" on the Elgi Rubber Company SELL** — matches the
  broker_imported same-day margin-netting case already fixed 2026-09-09
  (`_send_real_sell` checks `broker_imported` first and forces CNC). The
  order book's own Success list shows a *later* SELL of the same symbol/
  qty succeeding, consistent with a pre-fix rejection followed by a
  post-fix retry rather than a live gap. Not re-touched without more
  specific evidence this is a *new* case the existing fix doesn't cover.
- **"Rate Not Within Ckt Limit" on Graviss Hospitality** — a single
  occurrence, price only ₹0.11 above the upper circuit; consistent with
  an entry priced off a tick that was current a moment earlier, or a
  manual order. No repeat pattern in the evidence to justify a new
  circuit-limit pre-check without risking a speculative, unverified
  change to the pricing path.

## Carried over from session 21d (unchanged this round)

Fix 1-4 from `CHANGES_2026-09-10_SESSION21D_...md` (manual MARKET BUY
price, `/cycle/run` and `/manual-order/confirm` event-loop isolation,
`/positions/.../close` pending-SELL guard), plus this round's additional
event-loop-isolation fixes to `/positions/.../close`'s actual send,
`/orders/.../cancel`, and `/reconcile/{mode}` (all now run their
Dhan-calling body on a worker thread instead of the shared main loop, for
the same reason as `/cycle/run`).
