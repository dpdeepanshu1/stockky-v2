# Group 219 - scalp ranking v2 (position-stocks-service)

Cumulative on group 218. Item 4 of the 2026-10-07 loss/profit review. Rebuild: `docker compose build position-stocks-service && docker compose up -d`.
Files changed: `screening/engine.py`, `config.py` (6 settings), `tests/test_group219_ranking_v2.py` (new, 34),
`tests/test_screening_engine.py` (fixture pins the legacy formula).

## What the review found (re-checked in the code)
`score = pct_change x volume_weight x range x vwap x consistency x window`, with `volume_weight = min(day_volume / MIN_AVG_VOLUME, 3)`.
At the default floor of 50,000 shares the weight reaches its cap at 150,000, which almost every symbol passes soon after the open, so volume stopped
discriminating and the score reduced to "biggest mover first". That is the move most likely to be over. The quality gate then looks only at the top
`QUALITY_GATE_TOP_N` (3) of that list. That top-N is a latency design and is left alone; the fix is the ranking it is fed.

## Change (screening/engine.py)
`score = pct_eff x liquidity x volume_pace x range x vwap x consistency x window`
- `pct_eff = min(pct_change, SCAN_PCT_CAP_MULT x the window's threshold)` (default 3x: 3.0% on 5m, 2.1% on 1m, 4.5% on 15m, 7.5% on 60m). Size alone stops
  winning; the quality terms decide among strong movers. `Candidate.pct_change` is still the real move, and all downstream gates and levels still use it.
- `liquidity = min(day_volume / (3 x MIN_AVG_VOLUME), 1)`: the old weight normalised to at most 1, so thin names stay penalised and liquid ones are equal.
- `volume_pace` = this window's volume per minute divided by the symbol's own per-minute volume earlier in the session (volume before the window / minutes
  since 09:15 IST), clamped to 0.5-2.0. Unknown pace is neutral 1.0. Built from cumulative day-volume snapshots (one per 5 s, about 65 min deep) recorded in
  `on_tick_hook`. `Candidate.rvol` carries the pace (None = unknown). The `/candidates` JSON is unchanged, so it is not on screen yet; adding `rvol` to that
  endpoint is a one-line change if you want it.
- Unchanged: every gate (volume floor, spread cap, thresholds, open symbols), the range, VWAP, consistency and window multipliers, relax-when-under-preferred.
- `composite_score` is only stored and displayed (never thresholded), so its overall scale dropping by roughly 3x is harmless; compare scores only within a day
  of the same version.

| Env | Default | Meaning |
|---|---|---|
| `SCAN_RANKING_V2_ENABLED` | 1 | `0` restores the exact old formula |
| `SCAN_PCT_CAP_MULT` | 3.0 | cap on the move's contribution, in multiples of the window threshold; `0` disables |
| `SCAN_RVOL_MIN_WEIGHT` / `SCAN_RVOL_MAX_WEIGHT` | 0.5 / 2.0 | clamp for the volume-pace weight |
| `SCAN_RVOL_MIN_BASELINE_MIN` | 10 | minutes of session before a window starts needed to trust its baseline |
| `SCAN_RVOL_SAMPLE_S` | 5 | spacing of volume snapshots |

## Limits, stated plainly
- **This is not the time-of-day relative volume the review asked for.** True relative volume compares today's volume at this time with the symbol's usual
  volume at this time on previous days. This service has no such history, so v2 uses within-day pace. Because the opening hour is always heavy, pace tends to
  read below 1 later in the day for every symbol; since the same curve applies to all of them, the ranking among symbols is what it improves, not any absolute
  reading.
- Pace is unknown (neutral) for the first 10+ minutes of a window's baseline, for the 60m window until about 10:25, and after a restart until the snapshots
  reach back a full window. During those periods only the cap and liquidity change the ranking.
- **Nothing here is validated on outcomes.** There is no tick history in the zip to backtest against; the design follows the review's reasoning (the
  September file showed the five 09:19-09:24 entries all losing and one outlier carrying the profit, which is a small sample). It is ON by default. Check
  a few sessions of `/candidates/log` and trade results; `SCAN_RANKING_V2_ENABLED=0` is the immediate rollback.
- It changes WHICH candidate is tried first, not what a candidate must pass, so it does not lower any risk gate.

## Tests
- `tests/test_group219_ranking_v2.py` (34): the cap (above, at, below, disabled, per-window), liquidity at 1/3, 1/2, 2/3 and 1.0 of the cap, pace worked
  by hand (750k by 10:30, then 150k in 5 min = 3.0, clamped to 2.0; 0.4 clamped to 0.5; 1.25 used as is), every unknown case (no history, history too short,
  opening burst, volume reset, other symbol), snapshot spacing, bounds and fail-safety, and an end-to-end order: a steady +2.5% mover with pace 1.8 now outranks
  a +8% mover whose volume dried up (pace 0.6), while the legacy formula still ranks the +8% mover first.
- `tests/test_screening_engine.py`: its fixture now sets `SCAN_RANKING_V2_ENABLED=False`, so its existing 24 formula pins keep testing the rollback path unchanged.
- Sandbox: both files pass (109 with `engine.py` 97% covered). Whole position-stocks suite: 2835 passed, 3 failed, all in `test_group210_symbol_lock_sweep.py`
  and flaky (they fail on the untouched group 216 zip too, 3 of 32 in isolation there). api-gateway env sweep guards (27) pass.
