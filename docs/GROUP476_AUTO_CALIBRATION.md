# group 476 - Auto-calibration (Real Money Calibration)

Switches that have a test are now set from the test result. No manual toggling; every change is sent to Telegram.

## How it works
1. A test / pack run finishes in calibration-runner-service. If it contained a test listed below, `autocalib.py` evaluates every rule once.
2. For each rule it reads the latest usable result of the test (job run or pack run; failed / timed-out / older than 40 days = ignored).
3. The verdict maps to a value. It is applied through `POST <service>/control/flags/<KEY>` (so the service's own audit log has it too) and ONE Telegram
   message lists every switch that changed (switch, old -> new, why, which test run).

## Safety rules
- Only switches in `RULES` are touched. `PROTECTED` (broker stop, fail-closed data rules, order checks, evidence recorders, DEMO twins, 15:05 same-day entry) can never get a rule (the engine refuses to start with one).
- Tightening: one test run. Loosening: the same value from two DIFFERENT test runs in a row (a disagreeing or silent run resets the count).
- "not enough data / no edge / unstable" = no change. A test that is missing, failed or too old = no change.
- Numbers move at most 25 % per evaluation towards the supported value and stay inside the switch's range.
- A switch changed automatically is left alone for 24 h. Pin a switch on the page to never touch it. Master switch and dry run on the page.

## Switch -> test
| Switch | Service | Test | Rule |
|---|---|---|---|
| CANDIDATE_52W_LOG_ONLY | real-trade | legacy_gate | robust verdict `hurts` -> true (2 runs), `helps` -> false |
| KRONOS_GATE_MODE | real-trade | kronos_backtest | VETO HELPS -> enforce (2 runs); NOT PROVEN / VETO SEPARATES while enforcing -> log |
| TIMESFM_GATE_MODE | real-trade | tsfm_combine | Gate A PASS -> log, FAIL -> off |
| STRUCTURE_STOP_MODE | real-trade | gate_check | G3 PASS -> on (2 runs), FAIL -> off |
| STRUCTURE_EXIT_MODE | real-trade | gate_check | G2 FAIL and exit/tighten -> log (never promoted) |
| STRUCTURE_GATE_MODE / PS_STRUCTURE_GATE_MODE | real-trade / position-stocks | gate_check | G2 PASS -> enforce, FAIL -> off |
| STRUCTURE_MAX_CHASE_ATR, _MAX_STRETCH_ATR, _MIN_ROOM_ATR, _MIN_RR | real-trade | calibration_pack | stable walk-forward suggestion, only when every A/B verdict is `pass` |
| OVERNIGHT_HOLD_ENABLED | real-trade | hold_bucket | any multi-day bucket `worse` -> false. Never switched ON automatically (group 470 made off the default) |
| ENTRY_VWAP_GATE_ENABLED, OPENING_GATE_ENABLED | position-stocks | intraday_gates | `pass` -> true, `hurts` -> false (2 runs); recorded bars are position-stocks' |

Everything else is `manual` (no test decides it yet) or `protected`; the page lists every switch of both services with its status and reason.

## API (admin token)
`GET /autocalib/status`, `PUT /autocalib/settings {enabled, dry_run, pinned}`, `POST /autocalib/run {dry_run}`.
Env: `AUTOCALIB_ENABLED` (true), `AUTOCALIB_DRY_RUN` (false), `AUTOCALIB_COOLDOWN_H` (24), `AUTOCALIB_MAX_AGE_DAYS` (40), `AUTOCALIB_STEP_PCT` (25). State: `<RUNNER_DATA_DIR>/state/autocalib.json` (page settings win over env after the first change).

## Also in this group
- position-stocks `orders/close_all.py`: the two silent `except: pass` now log at debug (fixes `test_group322_no_new_silent_excepts`).
- real-trade `tests/test_group468_approve_first.py`: the fake `resilience.local_cache` is removed after every test (it broke 9 later tests: intraday_news, main startup, symbol_master).
- CI runs `tests/test_autocalib.py`.

## Follow-up (group 477): the calibration page vs the recent loss-review changes
- `real-trade-service/control/catalog.py`: `OVERNIGHT_HOLD_ENABLED` still said `shipped="true"` although group 470 made the code default false (the page showed a wrong default). Now `shipped="false"`, `recommended="false"`.
- New page switches for groups 470-472 (all read at call time, so they apply live): `STOPOUT_GUARD_ENABLED`, `STOPOUT_DAY_PAUSE_N`, `STOPOUT_SYMBOL_BLOCK`, `ENTRY_MIN_QTY`, `STOP_OVERSHOOT_ALLOWANCE_PCT`, `ORB_MODE`. They are PROTECTED from auto-calibration (decided by you, not by a report).
- Auto-calibration never switches `OVERNIGHT_HOLD_ENABLED` ON (only OFF when a multi-day bucket is worse).
- New test `tests/test_group477_catalog_matches_config.py` in both services: every catalog `shipped` value must equal the real config default (this is the check that would have caught the drift).
- CI now also runs `scripts/test_env_review_check.py` (group 475 had no CI entry).
