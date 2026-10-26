# E194 - real-trade exit: a blocked SELL wrote a new FULL_EXIT / EMERGENCY_EXIT audit row every cycle

Includes E193 (and E192 - E184).

## What I read
`services/real-trade-service/exit_engine/exit.py::evaluate_mode` from the top to the tally (expire stale exit orders, quotes, broker-stop sync, the per-position
loop: no-price, pending-SELL guard, emergency gap, stop, swing lane, target partial + DP guard, time stop, early warning, structure step, breakeven, ATR trail, hold,
the per-position error isolation), plus the helpers it relies on: `_write_exit_decision`, `_has_pending_real_sell`, `expire_stale_exit_orders`,
`_should_skip_exit_this_cycle`, the first half of `_send_real_sell`, `portfolio.open_positions`, `record_real_exit_sent`, and the tick validation in `market_feed/feed.py`.

## Finding (fixed)
`_write_exit_decision` throttles repeated HOLD rows (group368: one Oracle INSERT per open position per ~8 s cycle was slowing the only stop check), but every other action was written
every cycle. The loop writes the decision BEFORE calling `_send_real_sell`, and `_send_real_sell` returns False without calling Dhan in two known states:
1. the position is inside its rejection backoff (`consecutive_exit_failures` / `last_exit_failure_at`, up to `EXIT_RETRY_MAX_COOLDOWN_SECONDS`), and
2. it already hit today's intraday cutoff (`intraday_cutoff_hit_<id>_<day>` snapshot), which lasts for the rest of the day.
In both states the same `FULL_EXIT` / `EMERGENCY_EXIT` / `PARTIAL_EXIT` row was inserted again every cycle although nothing was sent: the DATAMATICS case (89 rejected SELLs over 4.5 h)
is exactly this, and a position stuck behind the 15:15 cutoff would add one such row per cycle until close. They also read as if exits had been sent. Reproduced: 6 cycles, 6 rows.

## Change (`exit_engine/exit.py`)
- `_exit_send_is_blocked(db, position)`: True only for a REAL position that is in backoff or has the cutoff snapshot (the same two predicates `_send_real_sell` checks first). Any error or doubt is False.
- `_write_exit_decision`: for `FULL_EXIT` / `EMERGENCY_EXIT` / `PARTIAL_EXIT` on a blocked position, an identical row (same action, same wording with numbers normalised) is written at most once per
  `EXIT_HOLD_AUDIT_SECONDS` (default 60, 0 = every cycle, the existing variable; no new variable). A different action or wording is written at once. The cycle in which the backoff has ended and the
  SELL really goes out is never "blocked", so it always has its row. DEMO and unblocked exits are unchanged.
- `tests/conftest.py`: the autouse audit fixture also clears the new throttle dict.

## Read and fine (no change)
Order of the checks (emergency, stop, swing, target, time stop, structure, breakeven, trail), the partial-exit arithmetic and the one-share case (the whole share is sold, the stop / target edit on the
closing position is harmless), the DP guard, breakeven / trail ratchets (never loosen the stop), the corporate-action ATR clamp, the per-position error isolation, `expire_stale_exit_orders` (the resend of the
unfilled remainder is what makes clearing the target safe). Ticks with price <= 0 or NaN never reach this loop (`feed.py`). Shorts do not exist in real-trade (position-stocks only).
Not changed, your call: the time stop counts calendar days (`.days`), so a Friday entry has 3 "held days" by Monday; and a stale last-good tick (up to `FEED_PRIORITY_STALE_FALLBACK_S`, 90 s, tagged in `source`)
is acted on by the exit loop like a fresh one.

## Tests
- `tests/test_e194_blocked_exit_audit_rows.py` (13): 13/13 pass on the fix; 9 fail on the unchanged E193 `exit.py` (the 4 that pass pin behaviour that must not change: an exit attempted every cycle keeps its row every cycle,
  throttle off keeps every row, per-position throttles, DEMO untouched).
- Whole real-trade suite here: 5,402 passed, 2 skipped, 4 failed. The 4 are `tests/test_oracle_compat.py::TestBuildOracleEngine` (`oracledb` is not installed in this sandbox; they do not touch this change).
- `docs/ENV_REFERENCE.md` regenerated (it embeds source line numbers); `--check` passes. No new environment variable.

    bash scripts/docker_test.sh real-trade-service
    python3 scripts/gen_env_reference.py --check
