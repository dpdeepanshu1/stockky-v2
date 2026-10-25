# E140 - swing replay: a Hold parameter, to test the one fair question left (2026-10-25)

## Decision and why
The E131 replay (5-session hold, 744 symbols, Rs 20,000/trade) gave **NO_EDGE**: net mean -0.411%, the ranker's edge over the other stocks (+0.23 pp gross) is smaller than the 0.296% round-trip cost. By the plan's own gate (S2: "If NO_EDGE, stop here. Nothing is traded") the 5-day swing lane does not go live. Nothing in live trading changed in this round: `SWING_MODE` stays `shadow`, `SWING_MAX_HOLD_DAYS` stays 5.

Why one more replay, not more building: published work on Indian stocks (Jegadeesh-Titman style, NSE/NSE-research papers) finds momentum profits at 3-12 month formation and holding periods, but **reversal** at a 1-month formation-and-hold horizon, and one study finds transaction costs alone can remove momentum / reversal profit. A 5-session hold sits in the reversal zone, which fits the NO_EDGE. Whether a longer hold (10-60 sessions) clears the cost is the only open, cheap, evidence-only question. It is academic evidence, not proof for your costs and size; the replay decides.

## Changes (replay tooling only; no production trading code touched)
- `services/calibration-runner-service/jobs.py`: job `swing_backtest` gets a **Hold (sessions)** parameter (5 default, 10, 20, 40, 60) passed to `swing_backtest.py --hold`. Every hold other than 5 uses its own checkpoint folder (`ckpt/swing_h<hold>_<seed>`), so it never overwrites the 5-session rows. Title shows the hold.
- `scripts/test_swing_backtest.py`: new test, `--hold 20` runs and the report says "held up to 20 sessions".
- `services/calibration-runner-service/tests/test_e140_swing_hold_param.py` (new, stdlib): the argv carries `--hold`, the checkpoint name differs per hold, the choices are offered.

## How to read the result
Same gate as before: only the declared primary (SCREEN setup, trail exit, Rs 20,000) carries the verdict and it must clear zero net with a day-clustered interval and no losing year. Look at the `hold` exit rows as well: the trail exit uses the lane's 2-6% ATR stop, which is tight for a 20+ session hold. Overlapping windows (step 3 with a 20-session hold) are strongly correlated, so use `step` 5-10 for 20+ sessions. Survivorship still flatters momentum. A USEFUL here would only justify a separate decision on a longer-hold lane (the 5-day limit is yours); it would not change anything by itself.

## Checked here
`test_swing_backtest.py` (14 tests, incl. the new one) and the new runner test run as plain scripts and pass; `py_compile` of `jobs.py`; repo guards pass. NOT run: the real replay (needs your history folder on the VM), the pytest suites (no network here).
