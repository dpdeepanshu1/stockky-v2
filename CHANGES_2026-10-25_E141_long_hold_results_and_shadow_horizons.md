# E141 - long-hold replay results (20 and 40 sessions) and forward check at 10/20 sessions (2026-10-25)

## VM results (744 symbols, Rs 20,000 per trade, round trip 0.296%)
| hold | step | primary net mean (SCREEN, trail) | 90% interval (days) | stopped | losing years | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| 5 (E138) | 3 | -0.411% | [-0.593, -0.222] | - | 2022, 2024, 2025, 2026 | NO_EDGE |
| 20 | 5 | -0.004% | [-0.305, 0.305] | 91.7% | 2022, 2025, 2026 | NO_EDGE |
| 40 | 10 | +0.030% | [-0.516, 0.579] | 98.9% | 2022, 2024, 2025, 2026 | NO_EDGE |

- The ranker still beats the other ranked stocks: +0.52 pp gross at 20 sessions, +0.50 pp at 40 (day-cluster interval above zero, "helps"). It is not enough to clear costs under the declared exit.
- **The declared exit is the problem, not necessarily the signal.** 92-99% of trades hit the 2-6% ATR stop, which is built for a 5-session hold; at 20-40 sessions the primary mostly measures the stop. Changing the primary after seeing the result would be data snooping, so it was NOT changed and the verdict stands.
- Exploratory only (no stop, exit at the last close): SCREEN +1.58% net at 20 sessions vs ALL +0.97% (+0.61 pp); at 40 sessions +3.83% vs +2.79% (+1.05 pp). But the ALL row itself is strongly positive, i.e. today's-symbols survivorship plus a rising market, and a no-stop hold is not what the lane does. Not a finding; needs re-testing on data the replay has not seen.

## Decision
- The swing lane does not go live at 5 sessions (three NO_EDGE replays); no longer-hold lane is built on this replay. `SWING_MODE` stays `shadow`, `SWING_MAX_HOLD_DAYS` stays 5. No trading code changed.
- The only survivorship-free evidence available is the lane's own saved evening rankings. E141 lets `swing_shadow_report` score them at **10 and 20 sessions** (`--hold 10|20`; runner job Hold choices 1/3/5/10/20; calendar windows 18 and 34 days). They need about 30 and 40+ saved trading days with an outcome, so expect NOT_ENOUGH_DATA until about mid-December and a usable 20-session answer around the end of December 2026. No stop is modelled and entry is the screening-day close.

## Changed
`scripts/swing_shadow_report.py` (HORIZONS, MAX_GAP_DAYS, docstring), `services/calibration-runner-service/jobs.py` (Hold choices and help of `swing_shadow_report`), new `scripts/test_swing_shadow_horizons.py` (4 tests, stdlib).

## Checked here
New tests, `test_swing_backtest.py`, the E140 runner test and the repo guards pass as plain scripts. NOT run: pytest suites (no network here).
