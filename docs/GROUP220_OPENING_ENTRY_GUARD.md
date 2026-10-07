# Group 220 - opening entry guard (real-trade-service)

Cumulative on group 219. Item 6 of the 2026-10-07 loss/profit review. Rebuild: `docker compose build real-trade-service && docker compose up -d`.
Files changed: `entry_engine/opening_guard.py` (new), `entry_engine/entry.py` (one early return in `evaluate_mode`), `execution/auto_pilot.py`
(ENTER_AT_OPEN trigger time), `config.py` (3 settings), `main.py` (one extra status key), `tests/conftest.py` (guard off for older tests),
`tests/test_group220_opening_guard.py` (new, 35), `tests/test_group220_enter_at_open_schedule.py` (new, 6).

## What the data shows (re-checked against `tier_breakdown.csv`) and what it does not
Entries opened 09:19-09:24 IST: ETERNAL -7.00, NUVOCO -176.80, WELSPLSOL -59.55, GICRE -28.80, ELGIRUBCO -2.09. That is 0 of 5 winning, -Rs 274.24,
over three trading days (2, 7 and 10 Sept). The other 17 trades made +Rs 301.03, of which IONEXCHANG alone is +Rs 290.80.

Why this is a weak basis, stated plainly:
- Five trades. The file's overall win rate is 7 of 22; five straight losses at that rate happens about 15% of the time by chance.
- There are no entries at all between 09:30 and 10:00 in the file, so it cannot say whether 09:30 or 09:45 is better, only that the open-minute entries lost.
- 4 of the 5 were base VOLUME_SHOCK entries, and Gate 1b has held that tier back by default since 18 Sept (`VOLUME_SHOCK_BASE_TIER_AUTO_ENTRY_ENABLED`). Only
  ETERNAL (PREPARE TO BUY) would still enter today. So with current defaults the guard adds less than the September file suggests.
- It is therefore a conventional precaution (the first minutes after the 09:15 open are the most volatile), not a measured fix.

One thing the review missed: two of the five entered at 09:19, before `ENTER_AT_OPEN`'s 09:20, so the regular auto-pilot cycle enters at the open too. A guard on
`ENTER_AT_OPEN` alone would not have touched them, which is why this lives in the shared entry path.

## Change
`entry_engine/opening_guard.py`. While the market is open and the IST clock is before `OPENING_ENTRY_NOT_BEFORE_IST` (default 09:30), `evaluate_mode()` returns
before its candidate loop: `{"evaluated": 0, "entered": 0, "waited": N, "rejected": 0, "entry_details": [], "opening_guard": "<reason>"}`. Nothing is consumed,
rejected or written as a decision, so the same candidates are evaluated normally, with live ticks, at the first cycle after the guard lifts. Logged at most once per 5
minutes per mode.

`ENTER_AT_OPEN` runs once a day. With the trigger at 09:20 and the guard at 09:30 it would fire into the guard, find every entry blocked, and spend the day's only
run. Its trigger time is now the later of `ENTER_AT_OPEN_TIME_IST` and the guard time (`/status` shows it as `effective_time_ist`); with the guard off or not covering
the mode nothing changes.

| Env | Default | Meaning |
|---|---|---|
| `OPENING_ENTRY_GUARD_ENABLED` | true | `false` turns the guard off |
| `OPENING_ENTRY_NOT_BEFORE_IST` | 09:30 | `HH:MM` IST; blank or invalid falls back to 09:30 |
| `OPENING_ENTRY_GUARD_MODES` | REAL,DEMO | comma list; set `REAL` to leave DEMO unguarded as a control for comparing outcomes |

## What it does not touch
- Exits, stops and square-off: `exit_engine` is untouched, so open positions are managed normally during the window.
- Manual orders through `manual_engine` are not guarded. A manual "Run Cycle" click IS covered, because the cycle calls `evaluate_mode(db, mode, gate_armed)` and the
  existing cycle tests pin that signature; it returns `waited: N` with the `opening_guard` message.
- The scalp service (position-stocks-service) has its own entry loop and is not covered; its entry-window decision (review item 8) still needs trade data.
- Not done: the open-gap check the review mentioned (skip a symbol that gapped hard against its previous close). It needs a per-candidate rule and a threshold I have no data to pick.
- Trade-off: names meant to be bought at the open, such as the overnight-priority list from `EOD_SIGNAL_SCAN` (the UPPER_CIRCUIT tier has a 69.7% backtested Day+1 win rate in the
  config notes), now wait until 09:30 as well. If you want them exempt, that is a small follow-up (an exempt-label list); I did not guess at it.
- Not checked: whether any candidate-expiry logic could drop a queued candidate before 09:30. Pre-pick already holds candidates from 09:00, so up to 15 more minutes is not new
  in kind, but I did not trace expiry.

## Tests
- `test_group220_opening_guard.py` (35): window edges (09:15, 09:19, 09:29 blocked; 09:30 onward allowed), pre-open/closed market, switch off, mode list, custom and invalid
  cutoff, a cutoff before the open, internal errors failing open, the real `tz_utils` calendar with explicit datetimes (Wednesday 09:20 blocked, Saturday not, UTC converted),
  the log throttle, `enter_at_open_time`, and `evaluate_mode` end to end: candidates stay unconsumed with no decisions written, the same candidates enter at 09:31, guard off enters
  at 09:20, DEMO enters while REAL waits, empty queue and pre-open cycles unchanged.
- `test_group220_enter_at_open_schedule.py` (6): no fire at 09:25 and the day's run is not spent, fires at 09:30, unchanged with the guard off or not covering the mode.
- `tests/conftest.py` turns the guard off for every older test, so none of them can flake when the suite runs between 09:15 and 09:30 IST.
- Sandbox: whole real-trade-service suite 3466 passed, 4 failed, 1 error. The same 4 failures and 1 error occur before this change (group 171 held-quote tests and a group 172 error,
  already on the P4 list). `opening_guard.py` 96% covered. api-gateway env sweep guards (27) pass.
