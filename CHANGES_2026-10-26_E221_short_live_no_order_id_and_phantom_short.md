# E221 - position-stocks: live short entry (`orders/short_live._attempt`) - no second SELL after an id-less answer, no phantom short

Includes E220.

## What I did
Read `short_live._attempt` end to end (guards, claim, capital, order, plain-order fallback, position save) and compared it with the long entry's recovery rules (E177/E180).

## Findings (fixed)
1. A Super Order answer with no order id was treated as a plain refusal. Dhan answered (so the SELL may exist) but `ambiguous` stayed False, so
   `plain_short.refusal_allows_fallback` allowed the plain-order fallback: a second SELL on top of a possible first one (double short). Without a fallback, the
   symbol claim was released and the symbol rested only 5 minutes, so the next cycle could sell it again. Now the answer is treated as ambiguous: Dhan's order book
   is searched (`_recover_ambiguous_sell(..., force=True)`); found -> tracked as the position's Super Order; book unreadable -> the existing E180 UNKNOWN path
   (claim kept, symbol rested for the session, operator alerted); nothing found -> ordinary failed order. In every case no plain SELL follows.
2. Phantom short. With Super Orders off, the guard checks `plain_short.entry_allowed()` early; the order step checks it again. If it turned false in between (a plain
   stop failed and tripped the day breaker), no order was sent but the code carried on and saved an OPEN SHORT with no order id. The exit side would then "cover" it
   with a real BUY, opening a long. Now "no SELL sent" is a refusal (`NO_ORDER_SENT`): capital and symbol lock released, nothing saved.

## Tests
`scripts/test_short_live.py`: +5 scenarios (78/78 pass; the first new one fails on the E220 code: it places a plain SELL on top). `tests/test_short_live.py`: +1 static test.
No config, env or frontend change.

## Run on your VM
`bash scripts/docker_test.sh position-stocks-service`, then restart position-stocks-service.

## Next unread
`_run_cycle`, `run_exit_reconciliation`, `short_entry.plan_short`.
