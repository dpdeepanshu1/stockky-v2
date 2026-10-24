# P2 item 13: trade-odds recorder - E103

Position Stocks, longs and shorts. RECORD ONLY: nothing is blocked, resized or ordered.

## What it does
`orders/trade_odds.py` stores `flow_json["odds"]` on every new position: `p_target`, `p_stop`, `p_open`, `n_resolved`, `be_win` (break-even win rate 1/(1+RR)), `avg_r` (average R before charges) and a one-line `verdict`, e.g. "target first 41% of 63 replays vs 33% needed: 8 pts ABOVE break-even (avg +0.23R before charges; one stock, one day, overlapping starts)".

How: a plain stop-versus-target formula gives zero edge by construction (a driftless walk hits the target 1/(1+RR) of the time, exactly break-even), so it replays the stock's own 1-minute bars so far today (`feed/bar_store`). Each earlier minute is a pretend entry at that bar's close; the stop and target (the position's own stop % and target %, mirrored for a short) are walked forward bar by bar; a bar touching both counts as a STOP; entries that reach neither are "open" and left out of the odds. Under 20 resolved replays (early in the day) only the counts are stored and the verdict says "not enough bars yet". No bars for the symbol, or bad levels: nothing is stored.

## Wiring
- `orders/entry.py` (long): right after the ORB recorder, before the position row is built; uses the same `levels.stop_pct` / `levels.target_pct` as the order.
- `orders/short_live.py` (live short): the position row now also gets `flow_json` (it had none before), with the odds for side SHORT.
- `config.py` `TRADE_ODDS_MODE` (`shadow` | `off`, ships `shadow`, anything else falls back to `shadow`); on the Switches page (group Evidence recording, read at call time); on the calibrator's PROTECTED list (a report may never switch it); line in `.env.example`.
- `GET /review-fixes/status` (the existing guard status route) shows `trade_odds_mode` and `trade_odds_counters` (recorded, too_few, no_bars, errors).
- The positions API already returns the whole `flow_json` through `order_flow.parse`, so `odds` appears there with no change.
- `tests/conftest.py` sets `TRADE_ODDS_MODE=off` for the suite (same as ORB) so no existing test sees a new key; the odds tests turn it on themselves.

## Proof
`tests/test_trade_odds.py` (16 tests, stdlib only, no SQLAlchemy): long and short on falling and rising bars, both-levels-in-one-bar = stop, exact touch, the last bar is never an entry, bad rows, too few resolved, break-even and average R maths, the three verdict wordings, merge keeps `orb` and `spread`, off / no bars / bad levels leave the input alone, never raises, and source rails (wired before the row is built, no database or network, nothing that orders). Six deliberate breakages of the module (wrong short stop side, short levels not mirrored, target checked before stop, last bar used as an entry, average R ignoring stops, wrong break-even) are each caught. Existing scripts still pass: `test_short_live.py` (47), `test_short_exit_paths.py` (61), `check_control_catalog`, `check_shared_copies`, `test_control_kit`, `test_regime_overlay`.

Not run here: pytest and SQLAlchemy (pypi blocked), so `python3 -m pytest tests -q` in position-stocks-service is for the VM.

## Not changed on purpose
Nothing reads the odds. Using them as a gate is P3 ("odds as a real gate") and needs the recorded trades first. The limits in the verdict text (one stock, one day, overlapping starts, no charges, resolved entries only) are real: treat the number as a comparison across many trades, not as a promise for one.
