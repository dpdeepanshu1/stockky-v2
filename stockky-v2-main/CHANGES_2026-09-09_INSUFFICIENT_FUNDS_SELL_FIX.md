# 2026-09-09 — SELL rejected "insufficient funds" on pre-existing holdings

## Symptom
Real exits for symbols the account already held in demat (before Stockky
started managing them) were being margin-rejected by Dhan:

    ⚠️ SELL rejected by Dhan — IDEA ×67 (manual_close)
    RMS:34126090760607:You have insufficient funds. Please add Rs.226.15 to trade.
    ⚠️ SELL rejected by Dhan — DEVYANI ×2 (stop_hit)
    RMS:321260907665407:You have insufficient funds. Please add Rs.72.44 to trade.
    ⚠️ SELL rejected by Dhan — PARADEEP ×7 (emergency_gap_down)
    RMS:321260907665507:You have insufficient funds. Please add Rs.237.99 to trade.

All three orders were tagged **Intraday** on the Dhan side, all three
positions were real, already-owned holdings, and the "funds required" was
consistently ~20–26% of the order's value — the shape of an *intraday
margin* shortfall, not a full-value shortfall. That percentage, plus the
Intraday tag, is what points at the root cause below rather than an
actual empty account (the account did not need anywhere near the full
order value to be "topped up" — it needed a small top-up because Dhan was
pricing a SELL of stock it already held as if it were a brand-new short).

## Root cause
`exit_engine/exit.py`'s `_send_real_sell()` — the single function behind
every automatic exit, manual sell, and manual-close (see
`manual_engine.py` and `main.py`'s `/manual-order/*` and
`manual_close_position`, all of which call it) — picks `product_type`
using:

```python
same_day_position = ist_today_str(as_aware(position.opened_at)) == ist_today_str()
sell_product_type = "INTRADAY" if same_day_position else "CNC"
```

That logic (added 2026-09-08 to fix the *CDSL eDIS* same-day rejection —
see `CHANGES_2026-09-08_CDSL_SAME_DAY_EXIT_FIX.md`) is correct **only**
when `position.opened_at` is the real date the position was bought. For a
position created by `portfolio.import_broker_holdings()` — the function
that pulls pre-existing Dhan demat holdings (bought manually, or already
sitting in the account before Stockky touched it — Vodafone Idea, Devyani
International, Paradeep Phosphates, Suzlon Energy in this account) into
Stockky's own `trade_positions` table — `opened_at` is set to the
**import timestamp**, not the purchase date, because Dhan's holdings API
doesn't expose the original purchase date at all.

So on the same day a holding got imported, `same_day_position` evaluated
`True` for a position that might have been held for weeks, and the SELL
went out as `product_type="INTRADAY"`. Dhan has no MIS (intraday)
position on that symbol to net an INTRADAY SELL against — only a CNC
holding — so it priced the order as opening a **fresh short**, which
requires margin. With real capital mostly sized against the CNC/delivery
value already committed, the account was short by exactly the intraday
margin percentage, not the full order value — hence the small,
per-symbol "add Rs.X" shortfalls instead of an outright block.

## Fix
1. **`models.py`** — added `TradePosition.broker_imported` (bool, default
   `False`). `True` only for positions created by `import_broker_holdings`.
2. **`portfolio/portfolio.py`** — `import_broker_holdings()` now sets
   `broker_imported=True` when creating the position row.
3. **`exit_engine/exit.py`** — `_send_real_sell()` checks
   `position.broker_imported` **before** the same-day/opened_at check: if
   `True`, `same_day_position` is forced `False` (so `sell_product_type`
   is always `"CNC"`), independent of when the row happened to be
   imported. Positions this system actually opened itself (via
   `entry_engine`/`manual_engine`) are unaffected — `broker_imported` is
   `False` for those, and the existing 2026-09-08 same-day/CNC logic
   applies exactly as before.
4. **`execution/dhan_client.py`** — added `is_insufficient_funds_error()`,
   matching Dhan's RMS margin-shortfall wording, alongside the existing
   `is_invalid_ip_error()` / `is_cdsl_edis_error()` detectors.
5. **`exit_engine/exit.py`** — `_send_real_sell()`'s exception handling
   gained a dedicated branch for `is_insufficient_funds_error()` (same
   per-position cooldown idiom as the CDSL branch) so this failure now
   produces a specific, explanatory alert instead of falling into the
   generic "SELL rejected by Dhan" branch with no indication of why a
   SELL of owned stock would ever need funds.
6. **`db.py`** — additive migration for the new column
   (`_ensure_position_columns`), **plus a one-time backfill**
   (`_backfill_broker_imported_flag`) that sets `broker_imported=True` on
   any pre-existing position whose `trade_position_events` row shows it
   was created by `import_broker_holdings` (`event_type="OPENED"`,
   `detail LIKE 'Imported from Dhan demat holdings%'` — a breadcrumb
   `import_broker_holdings` has always written). Without this backfill,
   the fix would only apply to holdings imported *after* this deploy —
   IDEA/DEVYANI/PARADEEP, already stuck in the rejection loop, would keep
   failing forever since their existing rows would default to
   `broker_imported=False`. Runs once at startup, idempotent (only rows
   still at the default get touched).

## Files changed
- `services/real-trade-service/models.py` — `TradePosition.broker_imported`
- `services/real-trade-service/db.py` — migration + backfill
- `services/real-trade-service/portfolio/portfolio.py` —
  `import_broker_holdings()` sets the flag
- `services/real-trade-service/exit_engine/exit.py` —
  `_send_real_sell()` product-type + error-handling changes
- `services/real-trade-service/execution/dhan_client.py` —
  `is_insufficient_funds_error()`

No changes needed in `manual_engine.py` or `main.py`'s
`manual_close_position` — both already route every real SELL through
`_send_real_sell()`, so manual sells and manual-close get the fix for
free, same as the 2026-09-08 CDSL fix.

## Verification
`models.py`, `db.py`, `portfolio/portfolio.py`, `exit_engine/exit.py`,
`execution/dhan_client.py` all compile cleanly
(`python3 -m py_compile`). The backfill query was checked against the
exact detail-string `import_broker_holdings()` writes
(`f"Imported from Dhan demat holdings (pre-existing, not bought via this "
f"app): {qty} @ avg cost ₹{avg_price}, ..."`) — the `LIKE 'Imported from
Dhan demat holdings%'` prefix match is stable across that f-string's
variable qty/avg_price suffix.

## What this does NOT fix
This was a code-path bug (wrong product_type for a specific class of
position), not an account-funding problem — no change here adds cash to
the account or bypasses Dhan's margin rules. A SELL can still be
genuinely margin-rejected if real margin is tied up elsewhere in the
account; that case now surfaces via the new dedicated alert in step 5
above instead of the old generic one, but still requires a human to
check the account, same as before.

## Addendum (same day) — "Insufficient Holding Quantity" surfaced next
Fixing product_type to CNC (above) got these SELLs past the margin check,
straight into Dhan's *next* gate: the CDSL eDIS/TPIN "Verify Holdings"
authorization that `execution/dhan_client.py` already had extensive
handling for under a different error string ("Validate Qty from CDSL",
see `CHANGES_2026-09-08_CDSL_SAME_DAY_EXIT_FIX.md`). This time Dhan
rejected with **"Insufficient Holding Quantity"** instead. Per Dhan's own
support article for that exact message, it means the same thing: *"the
scrip ... is not freely available in your holding"* — i.e. today's CDSL
authorization hasn't been done for that holding yet, not a real quantity
mismatch.

**Fix:** widened `dhan_client._CDSL_EDIS_MARKERS` to also match
`"insufficient holding quantity"` and `"scrip limit insufficient"`, so
`is_cdsl_edis_error()` now recognizes both wordings and routes to the
existing informative alert (open Dhan app → Verify Holdings → enter
T-PIN) instead of falling through to the generic "3 consecutive
rejections" escalation, which had no actionable next step.

This is not something a fully automated service can complete by itself —
it's a SEBI-mandated, OTP-to-your-phone step, same as the CDSL case
already documented. **Action needed once, before these four positions
(IDEA, SUZLON, DEVYANI, PARADEEP) can auto-exit:** call
`GET /dhan/edis/request-tpin` (SMS's you a T-PIN), then open
`GET /dhan/edis/authorize-form` in an actual browser (not curl — it
redirects to CDSL's page where you enter the T-PIN) with `bulk=true` to
cover all four holdings in one go. This authorization is valid for the
current trading day only and will need to be redone on any day these
positions are still open and need to exit — Dhan requires this daily, not
once-ever.

