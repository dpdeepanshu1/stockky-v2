# Group 255 - candle cooldown ladder and block-length log (2026-10-08 log with the group 254 counters)

## What the group 254 counters showed (boot 08:36 IST, market closed)
- Candle trip #1: `candle calls sent: 2 in the last 10s, 2 in the last 60s, 2 since start; quote calls sent: 8 in the last 10s, 97 in the last 60s`.
- Candle trip #2: `this 403 came 0.0s after the first candle call sent once the previous cooldown ended (1 call(s) sent since it ended)`.
- `/angelone/budget`: `candle_calls.sent_total 3`, `tripped_on_first_call_after_cooldown 1`, `throttle_events 0`, quote trips 0, quote calls about 45 a minute.

So AngelOne refused the candle endpoint after only 2-3 calls since boot, and refused the very first call after the 30 s cooldown again.
Our rate is not the cause (3 calls against 3/s, 180/min), and the quote rate (about 1.6 calls a second, well under the documented 10/s) did
not trip anything. The block lasts longer than our cooldown, or it was already in place before this container started.

## Changes (market-data-service)
- `angelone_budget.py`: candle cooldowns now climb 30, 60, 120, 240 ... up to `ANGELONE_CANDLE_COOLDOWN_MAX_S` (default 600; 60 restores the
  old ceiling). The escalation window for the candle family is 300 s plus the previous cooldown, so a long cooldown does not reset the
  ladder just by being long. Quote cooldowns are unchanged (30, then 60 at most).
- New `note_candle_ok()`: the first candle call answered normally after a run of 403s logs
  `candle calls are answered again, Ns after the first 403 of this run (candle trips so far: K)` and `GET /angelone/budget` shows it as
  `candle_calls.last_block_lasted_s`. That is the real length of AngelOne's block (an upper bound at cooldown granularity).
- `angelone_client.py`: `get_candles` calls it after a good answer.

## Effect
While the candle cooldown runs, `/history` is answered from the last-good daily series (group 230) or yfinance, exactly as before. The only
change is fewer pointless probes of a blocked endpoint (one call per cooldown), and a measured block length in the next log.

## Tests
`tests/test_group255_candle_ladder_and_recovery.py`, 14 tests (ladder values, restart after a quiet spell, env ceiling and bad env, quote
ceiling unchanged, recovery logged once with the length, run spanning several trips, no trip = no log, reset, never-raise, real
`get_candles` closing or not closing the run). Sandbox stand-in runner with stubbed httpx/pyotp; run `bash run_tests.sh` on the VM.
