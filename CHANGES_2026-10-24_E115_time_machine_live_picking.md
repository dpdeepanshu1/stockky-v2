# E115: Time Machine wired into LIVE stock picking (real-trade, position-stocks, Real Money Calibration)

It can only say NO. It never adds a trade, widens a stop or raises a size. Missing or slow data = no opinion and the entry goes on. Way back: `TM_GATE_MODE=log` (records, never blocks) or `off`.

## What it does
1. **Rank**: both shortlist scorers (Position Stocks long and short) and the Real Trade composite get +/- `TM_PICK_NUDGE_PTS` (6) when the stock's own recent 5-minute sessions AND a made-up-session simulation both favour / both oppose the rule. Cache only, never waits.
2. **Gate**: just before an entry is committed, this trade's own stop % / target % at this time of day is replayed. Verdict AGAINST (both negative after charges and slippage) refuses it.
   - Real Trade: new Gate 4h in `entry_engine/entry.py` (before the R:R floor), WAIT with `gate_tag=time_machine`, REAL and DEMO. Runs in a thread, waits at most `TM_GATE_INLINE_BUDGET_S` (2.5 s).
   - Position Stocks: long entry (`orders/entry.py`, skip `TIME_MACHINE...`), and `plan_short` (shared by shadow and live shorts, skip `SHORT_TIME_MACHINE`).
   - The verdict is stamped in `flow_json["tm"]` of every new position / decision.
3. **Warm, automatically**: `timemachine/gate_core.py` (byte-identical in both services, enforced by `check_shared_copies.py`) runs a worker plus a timer thread started at boot.
   - Timer: 09:05 data pre-fetch, first scan 09:16, then every 15 min to 14:30 (trading days; a missed boundary is not caught up).
   - Triggers: a shortlist built (both services, long and short), an entry attempt, the Market Weather 09:14 lock (runner calls `POST /time-machine/scan` on both services), runner pack `time_machine_open` scheduled 09:20 Mon-Fri (`s_tm_open`; also in the Monday open pack), `POST /time-machine/scan`, and a "Scan now" button.

## Real Money Calibration
- Eight switches on the page in BOTH services, group "Time Machine live picking": `TM_GATE_MODE`, `TM_GATE_BLOCK`, `TM_GATE_SIMS`, `TM_GATE_INLINE_BUDGET_S`, `TM_GATE_TTL_S`, `TM_PICK_NUDGE_PTS`, `TM_AUTO_ENABLED`, `TM_AUTO_INTERVAL_MIN`; the four `TIME_MACHINE_*` switches added to Real Trade. All on the auto-calibrator's PROTECTED list (your decision, after reading the refusal rate).
- New runner job `tm_gate_check` (script `scripts/time_machine_gate_check.py`): triggers a scan and reads both services: PASS / FAIL / NOT_ENOUGH_DATA. FAIL on no answer, wrong mode, no threads, or refusal rate over 50% of 10+ checks.
- Position Stocks Time Machine tab: "Live picking gate" card (mode, timer, counts, last checks, Scan now).
- New routes in both services: `GET /time-machine/gate`, `POST /time-machine/scan` (admin).

## Other
`timemachine/data.py`: market-data URL falls back to the env (real-trade keeps it outside `config`), session cache 96 stocks, optional per-call TTL (gate keeps sessions 30 min). `docs/ENV_REFERENCE.md` regenerated.

## Proof (run here)
position-stocks 4303 passed (one group356 timing test failed once under load, passes alone; the other three failures were mine and are fixed, re-run of those files green), real-trade 5309 passed (1 skipped), runner 240 passed, `npm test`, `tsc --noEmit` and `vite build` OK, catalog / shared-copies / env-reference guards OK. New tests: 25 (position-stocks), 8 (real-trade), 8 (runner).

## Not run here / to check on the VM
- Not run against live market-data: how many 5-minute sessions it serves (fewer than 10 = NOT_ENOUGH_DATA = no refusal).
- First live morning: `GET /time-machine/gate` on both services; `refusal_rate` and `recent_checks`. If it refuses too much, `TM_GATE_MODE=log`.
- Existing VM `schedule.json` keeps its old slots: add the 09:20 `time_machine_open` slot on the runner page (new installs get it). The in-service timer and the 09:14 weather trigger run regardless.
- Real Trade's replay assumes a same-day trade flat by 15:00 (true while overnight hold stays off); charges use the INTRADAY cost model.
- Rebuild: `docker compose build real-trade-service position-stocks-service calibration-runner-service frontend && docker compose up -d`.
