# Group 262 — one correct Dhan rate card for every charges figure, and net P&L after charges

## Symptom (group 261 screens)
Charges and total P&L did not add up across the three places that show them (Real Auto Trade Charges tab,
Charges-since-start table, Position Stocks Charges tab) and the Overview "Total P&L" ignored charges completely.
Concrete wrong lines on the screens: delivery BUYs showed STT 0.00, sells showed a flat DP of 13.5 with no GST,
every figure used a 0.00345 % exchange rate, and Position Stocks charged STT on both legs.

## Root cause
The rate card was hard-coded in **five** places (RealAutoTrade.tsx, PositionStocksTab.tsx, real-trade `charges_ledger.py`,
position-stocks `charges_ledger.py`, plus the cost gates reading differently-valued config defaults) and each copy had
the same mistakes. Dhan's published card (dhan.co/pricing) is:

| Charge | Correct | Was |
|---|---|---|
| Delivery STT | 0.1 % on BUY **and** SELL | SELL only (every CNC buy showed STT 0) |
| Intraday STT | 0.025 % on the SELL leg only | both legs (Position Stocks; Real MIS) |
| Exchange | NSE 0.00297 % + IPFT 0.0001 % | 0.00345 % (dashboards), 0.00325 % (config) |
| GST | 18 % on brokerage + exchange + IPFT + SEBI | left SEBI out |
| DP | Rs 12.50 + GST = Rs 14.75 per scrip sold from demat per day | flat 13.5, no GST (config said 15.0) |

Also: DP was billed on a sell that only closed shares bought the same day (nothing leaves demat), and a legacy SELL with
no recorded price was dropped from the report, losing its DP/STT. Position Stocks ledger rows were priced once at booking
and never revisited, so a later price repair left the charges stale.

## Fix
1. **One card, env-overridable.** `real-trade-service/config.py`: `EXCHANGE_TXN_PCT` 0.00297, new `IPFT_PCT` 0.0001,
   `DP_CHARGE_FLAT` 12.5. `cost_model.py` (entry cost gate) and `charges_ledger.py` read it. position-stocks `config.py`
   gets the same `EXCHANGE_TXN_PCT` / `IPFT_PCT`; `cost_gate.py` and `orders/charges_ledger.py` read it.
2. **Real ledger** (`real-trade-service/charges_ledger.py`): delivery STT both legs, intraday STT sell only, GST incl. SEBI,
   DP = 12.5 + GST once per scrip per day **and only for the part sold from demat** (same-day-bought shares are exempt);
   an unpriced legacy SELL is priced at the last buy of that symbol and flagged `estimated` (`orders_estimated`).
3. **Position Stocks ledger**: `charges_for_values()`; every read **restates** stored rows from their own buy/sell values with
   the current card (history self-corrects, no migration), and `book_positions` now refreshes a booked row whose position
   was later repaired. Periods expose per-component totals.
4. **Net P&L.** `GET /charges/{mode}/cumulative` returns `pnl` = booked realized P&L (account row) minus the charges.
   Overview: new "Total P&L after charges" card, "after charges" under P&L today and the total banner. Charges tab: today's
   headline now comes from the backend ledger (all orders, same card as since-start) when it has at least as many orders as the
   live list; new "Net realized P&L all-time (after charges)" block.
5. **Frontend single source**: `frontend/src/chargesRates.ts` (rates, `legCharges`, `chargesForLegs` incl. the DP rule) used
   by both tabs; per-trade cards and reference tables updated.

## Verified
- real-trade-service: 3774 passed (new tests for every rule above); position-stocks: 2910 passed.
- `npm run build` clean (zero TS errors).
- Pre-existing, untouched: `test_group210_symbol_lock_sweep::test_throttled_to_one_pass_per_interval` fails on the
  original zip too; `test_group172` has a fixture-teardown error in candidate_engine (`_HIST_REASON` is None).

## Not changed / limits
- Estimates: a Dhan contract note rounds STT and stamp duty to the rupee; DP is modelled once per scrip per day (Dhan's
  wording is "per instruction / ISIN"). Check a contract note if exactness matters.
- Position Stocks ledger still starts 5 Oct (older trades were purged before it existed).
- The ~Rs 0.95 TRIVENI gap: the card P&L came from an exit price that auto-repair (group 232) had not yet replaced with the
  order-book fill; the ledger now follows the repaired price, but the card itself still depends on that repair running.
- Real "Total P&L" % is still divided by the stored starting capital; funds deposited into Dhan are not netted out.
