# Group 399 - analysis from 09:10, entries from 09:15:00, structure exit live

Settings and default changes only. No gate, sizing, stop or risk-engine logic was edited.

## Timing
| Setting | Service | Old | New |
|---|---|---|---|
| `OPENING_GATE_MIN_MINUTES_AFTER_OPEN` | real-trade, position-stocks | 0.5 | 0 (no wait; entries are evaluated from 09:15:00) |
| `OPENING_FAST_START_IST` | real-trade | 09:14 | 09:10 |
| `OPENING_FAST_CYCLE_SECONDS` | real-trade | 20 | 10 |
| `PREOPEN_SNAPSHOT_TIME_IST` | real-trade, position-stocks | 09:08 | 09:10 |
| pre-open pass times (built-in list) | real-trade | 09:08 / 09:11 / 09:14 | 09:10 / 09:12 / 09:14 |
| `PREOPEN_MAX_PRICE_AGE_S` | real-trade, position-stocks | 180 | 420 |

- Position-stocks already built its opening plan at 09:10 (`OPENING_PLAN_TIME_IST`, group 391) and already allowed entries from 09:15 (`ENTRY_NO_BEFORE_IST`).
- Real-trade's `ENTER_AT_OPEN_TIME_IST` and `OPENING_ENTRY_NOT_BEFORE_IST` were already 09:15.
- From 09:10 the schedule loop wakes every 5 s (pre-open snapshot) and the full-cycle loop every 10 s. The full cycle itself still returns early until 09:15 (`is_market_open_ist`), so no entry can be placed earlier. The first cycle after the bell therefore starts within about 10 s of 09:15:00.
- The longer price-age limit is there because the 09:08-09:15 buffer sends no order updates, so a price used by the 09:14 pass can be several minutes old.
- Not changed: the opening gate (gap / range / opening-range checks until 10:00), opening protection (half size to 09:45, pause after 2 losing stop-outs), the pre-open gap block. They still apply to a 09:15 entry.

## Live
- `STRUCTURE_EXIT_MODE` now defaults to `exit` (was `tighten`): an in-profit position is sold on a fresh bearish daily-structure signal; a losing position is never touched. Set `STRUCTURE_EXIT_MODE=tighten` (stop only) or `off` to go back.
- Already live before this group and unchanged: structure policy (`STRUCTURE_POLICY_MODE=full`), P9 upgrade, real-trade / position-stocks structure gate, structural stop, board engine, opening plan and protection.
- Left off on purpose: `ENTRY_REGIME_GATE_ENABLED`, `MARKET_GATE_ENABLED`, `MARKET_GATE_PREV_CLOSE_ENABLED` (group 332 removed the weak-market blocks deliberately), `US_SECTOR_SIGNAL_ENABLED` (a ranking nudge the code says to prove in DEMO first).

## Verification
- Plain-Python: config defaults of both services, `preopen._pass_times()` and `preopen.due()` in both services, and the 6 runnable `TestMultiPass` cases of `test_group331_multipass_global_cues.py` pass.
- Not run (no pytest / sqlalchemy / httpx in the sandbox): the updated `test_group390_opening_fast_window.py`, `test_group268_opening_gate.py` (both services), `test_group365_structure_exit.py` and two httpx-based `TestMultiPass` cases. Run `docker_test.sh` for real-trade and the position-stocks suite.
- Not verifiable offline: whether the broker feed carries fresh prices between 09:10 and 09:15. Read the pre-open status coverage after the first morning.

## Corrections to older notes
- Group 382 says `BOARD_USE_MS_ENGINE` defaults to 0; `position-stocks-service/config.py` has defaulted it to on since group 354.
- Group 382 says weekly structure, Wyckoff, volume profile and V2 features do not exist; `stockky_ms/extras.py` (group 390) provides them as context only.

## Tests changed
`test_group331_multipass_global_cues.py` (pass times shifted to 09:10 / 09:12 / 09:14), `test_group390_opening_fast_window.py` (window 09:10, cadence 10 s), `test_group268_opening_gate.py` x2 (default 0, explicit 0.5 for the too-early case, new no-wait case), `test_group365_structure_exit.py` (fallback mode `exit`).
