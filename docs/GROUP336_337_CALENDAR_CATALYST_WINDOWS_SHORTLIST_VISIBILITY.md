# Groups 336 and 337 (2026-10-12)

Rebuilt from scratch in one session: the earlier sessions' files were never in the uploaded zip.

## 336 - calendar, catalyst, losing windows
| Rule | Where | Default | Switch |
|---|---|---|---|
| Expiry / pre-holiday / Friday entry cut-off | both services, `calendar_gate.py` | expiry 14:30, pre-holiday 14:30, Friday off | `CALENDAR_RULES_ENABLED`, blank cut-off = off |
| Smaller position on those days | both | x0.8 / x0.8 / x0.9 | `CALENDAR_*_SIZE_MULT` |
| Lower overnight-carry cap | real-trade only (`auto_pilot.py`) | 2 / 1 / 2, never above the normal cap | `CALENDAR_*_CARRY_MAX` |
| Big move with no news | position-stocks `catalyst_gate.py` | size x0.6 at >= 4 % | `CATALYST_NO_NEWS_ACTION` size/block/off |
| Pause a losing scan window | position-stocks `window_health.py` | >= 8 trades, < 35 % wins, net negative, 7 days | `WINDOW_AUTODISABLE_ENABLED` |
| Stop/target width advice | position-stocks `GET /tuning/stop-target` | advice only, needs 20 trades | `WINDOW_TUNING_MIN_TRADES` |

**Check before relying on it:** `CALENDAR_EXPIRY_WEEKDAY` defaults to 1 (Tuesday). Confirm against the current NSE circular. The holiday list is the one in `tz_utils.py`; keep it current as that file's own comment says.

In position-stocks a calendar cut-off ends the cycle (it is not symbol-specific); `CATALYST_NONE` (block mode) is symbol-specific, so the next shortlisted pick is tried.
In real-trade the cut-off is a Gate 4d WAIT (`gate_tag = calendar`), the size multiplier scales `adj_risk_pct`.

## 337 - shortlist visibility
- position-stocks `GET /shortlist`: picks with outcome, `evaluated`, `passed_quality_gate`, `entered_symbol`, `weights`.
- real-trade `GET /shortlist/{mode}`: picks (SELECTED / RESERVE) with composite, board / news / sector bonus, R:R, outcome.
- DEMO now uses the same Market Board nudge as REAL.
- Frontend: `ShortlistPanel` (both Board tabs) and `SessionHealthPanel` (Position Stocks Board tab).

## Verification status
Sandbox for this session had no network, so pytest and the service dependencies could not be installed:
- run: `py_compile` of every changed Python file; stubbed checks of the calendar / catalyst / window-health maths and of the real-trade shortlist state; the calendar module run against the real `config.py` and `tz_utils.py`; syntax-level TypeScript check of the new and edited frontend files.
- NOT run: the pytest suites (existing and the four new files) and `npm run build`. Run them before deploying:
  `cd services/position-stocks-service && python3 -m pytest -q tests` and `cd services/real-trade-service && python3 -m pytest -q tests`, then `cd frontend && npm ci && npm run build`.
