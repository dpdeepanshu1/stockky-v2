# E126: every Monday test that can run unattended now has a timer and a Telegram message

You asked whether all the tests of the real-money collaboration run at a set time and report to Telegram. Before this change five did not. Now all of them do, except T6, which stays manual on purpose.

## What was missing, and what is built

| Gap | Fix |
| --- | --- |
| The whole timetable shipped OFF (`RUNNER_SCHEDULER_ENABLED` false) | Default is now ON (`config.py`, `docker-compose.yml`). A `schedule.json` saved earlier with `enabled:false` still wins: run `schedon` (see below). |
| Wiring / health / snapshot ran on Mondays only | New pack `morning_open`, slot `s_morning_open`, Tue-Fri 08:50 (Monday keeps `s_monday_open`, so no day gets two). |
| The "09:30 live check" ran at 08:50 inside the Monday pack, when the market is closed, so it proved nothing | Removed from `monday_open`. New pack `live_check_open`, slot `s_live_open`, every weekday **09:35**. |
| The 10:15 live check (session must read `open`) was manual | New pack `live_check_1015`, slot `s_live_1015`, every weekday **10:20**. |
| T5 (short shadow rows, gate counters, ORB bar counters) was a manual `curl` | New job `shadow_shorts_check`, run inside the 09:35 pack. |
| T7 (no short open after the 14:55 cover) had no check | New job `shorts_flat_check`, pack `shorts_flat_close`, slot `s_shorts_flat`, **15:05**. FAIL names the symbol. |
| Nobody checked the Market Weather overlay was gone after 15:30 | New job `overlay_clear_check`, pack `overlay_clear_close`, slot `s_overlay_clear`, **15:35**. |
| `short_shadow_report` was in no pack | Added to `friday_review`. |

All three new jobs are one read-only script, `scripts/intraday_state_check.py` (`--phase shadow-shorts | shorts-flat | overlay-clear`). It only reads, skips weekends and exchange holidays (from market-data `/market/session`, `calendar_holiday`), and ends with `<NAME>: PASS | FAIL - <reason> | SKIPPED - <why>`.

## Telegram

Nothing new to wire: the runner already sends one message for every scheduled run (OK, LOOK, FAILED, BLOCKED), and a slot that could not start is announced too. The new packs reuse that, so a clean day gives you an "all clear" message at 09:35, 15:05 and 15:35, and a failing check gives a high-priority one with the failing line. Undelivered messages are kept and retried for 24 hours (group 466, unchanged).

## Not automated, on purpose

- **T6 Dhan SELL probe** places a real 1-share order and needs the tick box. A test (`test_no_pack_places_a_real_order`) pins that no pack contains it. Telegram still reports its PASS or FAIL.
- **T8** (raising `SHORT_LIVE_QTY_CAP`) and flipping `SHORT_MODE` / `MARKET_STATE_MODE` stay yours.

## Daily timetable (IST, Mon-Fri)

08:00 weather morning brief, 08:50 Monday open pack (Mon) or morning open pack (Tue-Fri), 08:50 weather refine, 09:14 weather lock (+ Time Machine scan), 09:20 Time Machine gate, 09:20-15:25 weather ticks every 5 min, 09:35 live check + T5, 10:20 live check, 15:05 T7, 15:30 overlay ends, 15:35 overlay check, 16:05 daily close pack, Fri 16:40 Friday review (now with the short shadow report).

## Tests

- `scripts/test_intraday_state_check.py`: 8 tests (pass, each failure, unreadable service, holiday / weekend skip, fail-open when market-data is down, exit codes).
- `services/calibration-runner-service/tests/test_e126_test_automation.py`: 9 tests (slots point at real packs and are on, exact times and days, Monday not doubled, Monday pack lost the premature check, builders, scheduler fires each slot in order on a Tuesday and not on a Saturday, an older `schedule.json` gets the new slots once, defaults, scheduled PASS and FAIL reach Telegram).
- Updated on purpose: `test_main_routes.py` (11 packs, timetable starts ON), `calibration.test.mjs` (11 packs; id pattern now allows digits).
- Run here: runner suite 255 passed; the new script tests 8 passed; `calibration.test.mjs` ok; merge-marker, secrets, ports, table-name and shared-copy guards pass. NOT run here: the other service suites, `check_control_catalog.py` (timed out here, no switch was added), the Docker builds, anything against Dhan or market-data.

## On the VM

```bash
cd ~/stockky-v2 && docker compose build calibration-runner-service frontend && docker compose up -d
source scripts/vm_runjob.sh && runlogin
schedon          # switches the timetable on if an old saved file had it off, then lists every slot
runjob '{"kind":"pack","target":"live_check_open","params":{}}'     # try one new pack by hand (market hours)
```
