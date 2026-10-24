# E114 - plain-order short fallback with a broker stop (plan open item 1) - 24 Oct 2026

Position Stocks only, shorts only. Real Trade and every long path are untouched. Built live-capable (your standing rule); it still cannot place anything until
`SHORT_MODE=live`, which still needs the T6 probe PASS.

## What it does
If Dhan refuses the SELL Super Order (or `USE_SUPER_ORDER` is off) and `SHORT_PLAIN_FALLBACK` is on (ships `true`, protected from the calibrator), `short_live.py` hands the
short to the new `orders/plain_short.py`:

1. plain INTRADAY MARKET SELL, wait up to `SHORT_PLAIN_FILL_WAIT_S` (8 s) for the fill in Dhan's order book;
2. a BUY `STOP_LOSS_MARKET` at the stop price, placed and READ BACK (`dhan_client.place_intraday_buy_stop`, same blocking read-back as the overnight stop). Stop and target are
   re-anchored on the real fill price and tick-rounded;
3. **no stop = no short.** If the stop cannot be placed the short is covered at once (BUY MARKET, up to 3 tries) and the **day breaker** trips: no further plain short today;
4. if even the cover fails the short is saved WITHOUT a stop id (Telegram critical) and the guard covers it on its next pass.

Never falls back for: restricted stock, circuit limit, intraday cut-off, no funds (a plain SELL would meet the same wall), or an ambiguous error (the Super Order may exist).

## State unknown = nothing bought
If the order book cannot say whether the plain SELL filled, no stop and no BUY is sent (a BUY with no short under it would open a LONG). The SELL goes on a watch list
(capital + symbol lock stay held): once seen filled it is covered with a BUY, once seen dead (or absent for 2 minutes) the claims are released. The watch list is in
process memory: after a restart check Dhan by hand (the Telegram alert says so).

## After entry: `plain_short.run_plain_short_guard` (fast reconcile loop, after the stop watchdog)
| Case | Action |
| --- | --- |
| broker stop filled | `STOP_HIT` booked at the real fill, capital and lock released |
| broker stop dead (rejected / cancelled / expired) | ONE re-place, then cover by hand (`STOP_BREACH_EXIT`) |
| no stop on record | cover by hand, critical alert once |
| price at or under the target | `TARGET_EXIT` (new status): stop cancelled and read back, then BUY MARKET. A plain short keeps its first stop (no trailing) |
| price past the stop for `SHORT_PLAIN_STOP_GRACE_S` (6 s) and the stop silent | cover by hand (`STOP_BREACH_EXIT`) |
| a cover BUY already exists at Dhan | adopted (left PENDING_RECONCILE for reconcile to price), never doubled |
| stop filled in part | live: wait (never cover on top); then dead: critical alert, check Dhan (not auto-booked) |
| book unreadable | no BUY sent blind; retried in 5 s |

The guard keeps running if you switch `SHORT_PLAIN_FALLBACK` off (the switch gates NEW plain shorts only).
The 14:55 cover, the 15:00 sweep, manual exit and close-all go through `eod_squareoff._fire_flat_sell`, which now calls `release_stop_before_cover()` first: resting stop
cancelled and read back; a stop that already filled raises `PositionAlreadyFlat` (no BUY on top of it); a stop part-filled or not cancellable blocks the BUY and the caller retries.

## Files
- new `orders/plain_short.py`; `execution/dhan_client.py` (`place_intraday_buy_stop`, `get_order_row`); `orders/short_live.py` (fallback, row with `dhan_super_order_id=None`,
  `plain_stop_order_id`); `orders/eod_squareoff.py` (release before cover); `main.py` (guard in the fast loop, read-only `GET /short/plain`);
  `models.py` + `db.py` (`scalp_positions.plain_stop_order_id`, auto-migrated, nullable); `orders/statuses.py` (+`TARGET_EXIT`); `config.py`, `control/catalog.py`,
  runner `autocalib.py` protected list (`SHORT_PLAIN_FALLBACK`; the fill-wait 8 s and stop-grace 6 s are env-only knobs `SHORT_PLAIN_FILL_WAIT_S` / `SHORT_PLAIN_STOP_GRACE_S`); frontend `positionStocksApi.ts`, `PositionStocksTab.tsx` (`TARGET_EXIT` colour).
- tests: `scripts/test_plain_short.py` (61 cases), `scripts/test_short_live.py` (67, 20 new for the fallback), `tests/test_plain_short.py`, `tests/test_short_live.py` (rails updated: the fallback lives only in `plain_short.py`).

## How it was checked (honest)
No network here: no pytest, no SQLAlchemy, no Dhan. The behaviour scripts fake the broker, DB, ledger and lock (same method as `test_short_live.py`). 10 deliberate breakages
(partial-stop check, breaker, stop-filled-while-cancelling, switch gating the guard, cover lookup, orphan cover, ambiguous fallback, missing watch, wrong trigger) were each caught;
one was an equivalent mutant. Stub-harness run of every importable test module in position-stocks and the runner: no test that passed before fails now (position-stocks 338 -> 344 passing;
the 51 stub-only failures are the same before and after). **Not run against real Dhan:** what Dhan answers to a BUY `STOP_LOSS_MARKET` INTRADAY, and whether a MARKET SELL for a restricted
stock is accepted, only the VM shows (see below).

## Decisions taken (research, no questions asked; each has a way back)
- Day breaker after one stop failure: a stop that cannot be placed once is likely to fail again (trigger rule, margin); one round trip lost is the cap. Way back: restart clears it; or `SHORT_PLAIN_FALLBACK=false`.
- Stop is never trailed on a plain short: modifying a resting SL order races with its own fill; software trailing would need a cancel/replace window with no stop. Way back: none needed; Super Order shorts still trail.
- One stop re-place only, then cover: a stop that dies twice is not trustworthy.
- Grace 6 s (same order of size as the long watchdog) for a silent stop.
- Target is software (cover by BUY at the tick): there is no broker target on a plain order.

## On the VM (your side)
After deploying: `curl -s http://localhost:8006/short/plain` (or `https://stockky.duckdns.org/positionstocks/short/plain`) -> `fallback_switch: true`, `entry_allowed_now: true`, counters 0.
On T7 (first 1-share live short) the plain path only runs if Dhan refuses the Super Order. If you want to see it once on purpose, run T7 with `USE_SUPER_ORDER=false` on a 1-share cap day;
then send me `docker logs position-stocks-service 2>&1 | grep -i "plain short\|short live"` and the Dhan order book rows for that symbol (SELL, BUY SL-M, cover).
