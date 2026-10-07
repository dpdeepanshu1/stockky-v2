# Group 221 - scalp market gate sees the previous close (api-gateway + position-stocks-service)

Cumulative on group 220. Part of item 8 of the 2026-10-07 loss/profit review (the Nifty-gate half; the 09:30-14:30 entry-window half is still waiting for trade data).
Rebuild: `docker compose build api-gateway position-stocks-service && docker compose up -d`.
Files changed: `api-gateway/main.py` (`_prev_session_change`, two extra keys in `/market/indices`), `position-stocks-service/screening/trade_gates.py`, `config.py` (2 settings),
`main.py` (2 status keys), `api-gateway/tests/test_group221_indices_prev_close.py` (new), `position-stocks-service/tests/test_group221_prev_close_gate.py` (new, 34).

## What the review found, and the cause
The scalp market gate blocks entries while Nifty is at or below -0.10% **against today's open**. The review called that out as wrong. It is, but the reason is a bug in the
gateway, not a design choice: `/market/indices` reads `history(period="1d")`, which is one row, so its `len(hist) > 1` previous-close branch never runs and
`nifty.change_pct` falls back to the open. The code, its comments and the existing two-row tests all describe a previous-close change; production has only ever produced an
open-based one.

Concrete hole: Nifty gaps down 1.2% at 09:15 and then trades flat. Against the open that is 0.0%, so the gate lets every scalp through on what is plainly a weak day.

## Change
- **Gateway** (`_prev_session_change`): a second read, `history(period="5d")`, gives the real previous session close. `/market/indices` now also returns
  `nifty_vs_prev_close` and `sensex_vs_prev_close` as `{"prev_close": ..., "change_pct": ...}`. If the last row is dated before today (pre-open, weekend, holiday) that row is the
  previous close; otherwise the second-last row is. Any failure drops only the new key. One extra yfinance call per index per refresh (the payload is cached 300 s).
- **Scalp gate**: after the existing "vs day open" check, a second check blocks when `nifty_vs_prev_close.change_pct <= MARKET_GATE_MIN_NIFTY_PREV_CLOSE_PCT`. The reject text
  starts with `MARKET_WEAK` as before and now says `vs prev close`. It ignores a `stale` or `fallback` gateway body (yesterday's number must not gate today), a missing key (older
  gateway) and any fetch failure, so it fails open like the rest of the gate. The existing open-based check still runs first and is untouched.

| Env (position-stocks-service) | Default | Meaning |
|---|---|---|
| `MARKET_GATE_PREV_CLOSE_ENABLED` | 1 | `0` restores the old gate exactly |
| `MARKET_GATE_MIN_NIFTY_PREV_CLOSE_PCT` | -0.75 | block at or below this change vs previous close |

**The -0.75 is my assumption** (roughly a bottom-fifth Nifty day), with no outcome data behind it. Tune it from the `MARKET_WEAK` lines and the day's results.

## Deliberately not changed
- `nifty.change_pct`, `nifty.change`, `sensex.*` and `market_score` / `market_mood` still use the open-based figure. real-trade-service's regime gate, the dashboard and the
  thresholds you tuned (for example the 2026-09-03 sensitivity change) were all built on those values. Switching them to the true previous-close change would shift live REAL
  entry decisions on gap days, in both directions (a gap-down that recovers would score lower, a gap-up that fades would score higher). I did not want to move that without data.
  **Decision for you:** if you want the displayed change and the regime score to be the textbook previous-close change, say so and it is a small follow-up.
- The scalp gate's own open-based check is kept: for a scalp, "Nifty falling since the open" is a meaningful intraday signal on its own.
- real-trade-service does not read the new keys.
- The ENTERED log line and `/candidates` output are unchanged.

## Trade-off
On a day Nifty is down more than 0.75% from the previous close, scalp auto-entries stop for as long as that holds, even if the index is recovering from the open. Manual
`/cycle/run` bypasses the gate, as before.

## Tests
- `position-stocks-service/tests/test_group221_prev_close_gate.py` (34): defaults, payload parsing (stale, fallback, missing, NaN, inf, text), the gap-down-flat-since-open case,
  inclusive boundary, open-based reason first, open change missing, both switches, env tuning, cache, a refetch clearing an old value, the real fetch against a fake gateway
  (good, older body, stale body, 503, exception), end to end.
- `api-gateway/tests/test_group221_indices_prev_close.py`: `_prev_session_change` (market hours, gap day, pre-open/holiday, integer index, NaN rows, one row, empty, no Close
  column, bad numbers, failing call) and the endpoint with production-shaped data (one-row 1d frame, multi-row 5d frame): open-based fields and score unchanged, new keys
  present, cached with the payload, one 5d read per index, a failing 5d read drops only that key, zero-fallback payload has no key.
- Sandbox has no pytest, fastapi, sqlalchemy or httpx. What I ran: the 34 scalp-gate tests under a small pytest stand-in with stubbed `httpx`/`sqlalchemy`/`models` (34 passed; a
  `<=` to `<` slip in the gate is caught by the boundary test), and 18 cases of `_prev_session_change` run against the real function source. The gateway endpoint tests were
  **not run** here (they import `main`, which needs fastapi) - please run `bash run_tests.sh` on the VM. The existing `/market/indices` tests are unchanged and should still pass: the
  `nifty`/`sensex` blocks and score are computed exactly as before, and the existing fake `Ticker.history(period=None)` accepts the new `period="5d"` call.
