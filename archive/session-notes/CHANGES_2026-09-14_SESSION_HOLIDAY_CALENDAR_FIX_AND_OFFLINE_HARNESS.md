# 2026-09-14 — Holiday-calendar live incident fix + offline pipeline test harness

## Incident

Today, 2026-09-14, is Ganesh Chaturthi — a gazetted NSE/BSE trading
holiday (verified against NSE's official 2026 circular, cross-checked via
Zerodha's holiday calendar). The exchange was closed all day. Despite
that, position-stocks-service's background auto-pilot loop kept running
full cycles, and notification-scheduler-service sent its normal scan/open/
close notifications, as if it were a regular trading Monday.

## Root cause

`is_market_open_ist()` (in both `position-stocks-service/tz_utils.py` and
`real-trade-service/tz_utils.py`) only ever checked weekday + 09:15–15:30
IST hours. It had **zero exchange-holiday awareness** — this was already
flagged in its own docstring as a "Phase 3 TODO", but nothing had wired a
holiday check into it yet. Since 2026-09-14 is a Monday inside market
hours, the function returned `True` all day.

Separately, `notification-scheduler-service/scheduler/run_once.py` DOES
have an `is_holiday()` check — but its `HOLIDAYS_2026` list was stale and
wrong (e.g. `"2026-03-02"` for Holi, which actually falls on `2026-03-03`;
`"2026-04-02"` and `"2026-04-10"`, which aren't holidays at all;
`"2026-10-22"` for Dussehra, which actually falls on `2026-10-20`) and,
critically, **did not include 2026-09-14 at all**. Same problem
independently in `api-gateway/nse_holidays.py`'s `_NSE_HOLIDAYS` set.

So there were four separate copies of "the 2026 holiday list" across the
repo (no shared import path between services — each is a separately
deployed container), and all four had drifted apart, and all four were
missing today's date.

## Fix

1. `services/position-stocks-service/tz_utils.py` and
   `services/real-trade-service/tz_utils.py`: added a verified
   `_NSE_HOLIDAYS_2026` set and wired it into `is_market_open_ist()` (plus
   a new `is_nse_holiday()` helper). This is the single highest-leverage
   fix — `is_market_open_ist()` gates `position-stocks-service/main.py`'s
   `_trading_loop`, and in real-trade-service it gates `cycle_runner.py`,
   `execution/auto_pilot.py`, `watchlist_engine/dynamic_universe.py`,
   `entry_engine/entry.py`, and `manual_engine.py` — fixing it once here
   fixes every one of those call sites.
2. `services/api-gateway/nse_holidays.py`: replaced the 2026 entries in
   `_NSE_HOLIDAYS` with the verified list (dropped guesses like
   `"approx"`-labeled dates, added the missing ones).
3. `services/notification-scheduler-service/scheduler/run_once.py`:
   replaced `HOLIDAYS_2026` with the same verified list.
4. `scripts/check_holiday_lists_sync.py` (new): parses all four files and
   fails loudly if their 2026 date sets don't match exactly. Run this
   after editing any one of them. Confirmed passing as of this fix — all
   four agree on the same 16 dates.

The verified 2026 NSE/BSE trading-holiday list (cross-checked against
NSE's official circular via Zerodha's holiday calendar):

```
2026-01-15  Maharashtra Municipal Corporation elections
2026-01-26  Republic Day
2026-03-03  Holi
2026-03-26  Ram Navami
2026-03-31  Mahavir Jayanti
2026-04-03  Good Friday
2026-04-14  Dr. Ambedkar Jayanti
2026-05-01  Maharashtra Day
2026-05-28  Bakri Eid (Eid ul-Adha)
2026-06-26  Muharram
2026-09-14  Ganesh Chaturthi   <- the one that was missing everywhere
2026-10-02  Gandhi Jayanti
2026-10-20  Dussehra
2026-11-10  Diwali Balipratipada
2026-11-24  Guru Nanak Jayanti
2026-12-25  Christmas
```

(Independence Day, Aug 15 2026, falls on a Saturday — already excluded by
the weekday check, correctly not listed as a separate weekday holiday.)

## Still true / unresolved

The underlying architectural issue — no shared import path between
services, so this list must be hand-copied into four places every year —
is unchanged. `scripts/check_holiday_lists_sync.py` catches drift after
the fact; it doesn't prevent someone from editing only one copy. If a
genuinely shared config service/package ever becomes feasible, this is
the clearest candidate to move there first.

## Also this session: offline pipeline test harness

New script: `scripts/test_position_stocks_pipeline_offline.py`. Lets you
run the REAL `screening/engine.py` + `screening/quality_gate.py` against
real historical NSE bars (via yfinance) with no live WS feed and no
market hours required — timing every stage, and printing every
window's pct-change against the real config.py thresholds (including
near-misses, not just candidates that fully cleared every gate), so the
0.5/1.0/1.5/2.5% window floors and 40/40 quality-gate floors can actually
be evaluated against real signal instead of guessed at. See the script's
own module docstring for the full REAL-vs-SYNTHETIC breakdown (bar data
and thresholds are real; tick density is synthetically upsampled from
1-per-minute bars and is explicitly flagged as illustrative).
