# E242 - Dhan SELL probe: Dhan's POSITIONS decide whether a short is open (2026-10-28)
Roll back: restore `services/position-stocks-service/orders/sell_probe.py`, `.../execution/dhan_client.py`, `scripts/dhan_sell_probe.py` and the two test files from the E241b zip.

## What happened (real run, 09:41 IST, job dhan_sell_probe)
SELL 1 ITC Super Order accepted (id 23326102814775). The Super Order list said PENDING for the whole 20 s poll, so the probe called it "never filled", cancelled all three legs and sent NO cover
("nothing to cover"). At 09:43 Dhan's Positions showed **ITC, S, qty 3, avg 254.03, Intraday**, and Stockky's Positions tab showed nothing (0/5 open, 0 closed). So a real short
existed at the broker with every leg cancelled (no stop) and no owner in Stockky.

## Root cause (code)
`sell_probe.py` decided "is a short open" from one source, the Super Order list, in three places: the poll, the post-cancel check, and the adopt condition (`fill is not None`).
When that list did not show the entry as filled, the probe sent no BUY **and adopted nothing**, whatever the account really held. It also never looked at what the symbol already held.
(Why Dhan's list stays PENDING while the position exists is NOT proven here: that needs the raw row. The run now prints it - below.)

## Fix
- `dhan_client.get_positions(db)` (position-stocks): read-only, "no positions" = empty list.
- Before any order the probe reads positions. The symbol must be **flat** (any row, any product); a symbol with an old position (like the 3-share ITC short) is skipped and the next default symbol is tried; unreadable positions = SKIPPED, nothing placed. This also means a pre-existing short can never be mistaken for the probe's fill.
- During the poll it reads positions too. A short seen at Dhan while the list shows no fill is treated as filled (priced from positions `sellAvg`, else the order book, else the ref price), **covered**, and reported as a FAIL "the Super Order list did not show the entry as filled although Dhan's positions show it" - because the live short's reconcile reads that same list.
- After the cancels the BUY decision comes from the position: BUY only while the book is short beyond the baseline; quantity is capped at the probe's 1 share; a lagging position gets two extra looks before "flat" is believed; a filled row over a flat book sends NO BUY (it would open a long) and is reported; unreadable positions with no fill seen send no blind BUY and say CHECK DHAN.
- After the BUY fills, positions are read again; a short still showing = cover not confirmed (adopted + Telegram, as before).
- Adoption now also fires when the short was seen only in positions, so the Positions tab shows it instead of nothing.
- Runner `scripts/dhan_sell_probe.py`: on a FAIL it prints `raw   <name>: {...}` lines (row after fill, row after cancel, positions after cancel, cancel answers), so the exact field Dhan used can be read from the run. A PASS prints as before.

## Tests (run here against stand-ins for SQLAlchemy; no pytest-with-DB in the sandbox)
- `tests/test_sell_probe.py`: FakeDhan now keeps positions. 8 new tests (list lags but position short -> covered and reported; same with cover failing -> adopted; old position blocks; default moves to next flat symbol; unreadable positions refuse; positions lost after the order -> no blind BUY; filled row over flat book -> no BUY; cover checked against position). 35 of 36 pass in the stand-in harness; the 36th (`test_live_short_is_refused_until_a_pass_is_on_record`) imports `capital.ledger`, which needs real SQLAlchemy, and is unchanged by this fix. Mutation: with the position reads removed, 16 tests fail, including all the new ones.
- `calibration-runner-service/tests/test_dhan_sell_probe.py`: 9 pass (1 new for the raw lines).
- Please run on the VM: `cd services/position-stocks-service && pytest tests/test_sell_probe.py tests/test_short_live.py -q` and `cd ../calibration-runner-service && pytest tests/test_dhan_sell_probe.py -q`.

## Still open / yours
- **Close the 3-share ITC short in Dhan** (Positions -> exit). The probe cannot and should not touch a 3-share position (its quantity is fixed at 1).
- Re-run the job after rebuilding position-stocks-service and calibration-runner-service. If it FAILs again, send the whole output: the `raw` lines will show what Dhan's row really says.
- A pre-existing limit: when the probe has to adopt an uncovered short, its legs were already cancelled by the probe, so the adopted row has no broker stop; the 14:55 cover and the stop watchdog's software check own it. Not changed here.
