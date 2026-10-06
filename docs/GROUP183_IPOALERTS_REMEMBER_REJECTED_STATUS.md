# Group 183 - ipoalerts: stop asking for a status the free plan rejects (api-gateway)

Cumulative on group 182. Source: your 2026-10-06 boot log, line
`ipoalerts status=listed -> HTTP 400: ... "This parameter is not supported for free plan users."`.
Rebuild: `docker compose build api-gateway && docker compose up -d`. Only `services/api-gateway/ipo_scanner.py` changed (plus tests).

## What was wrong
`fetch_ipoalerts_calendar()` asks ipoalerts for two statuses, `open` and `listed`. On a free plan `listed` is always
answered with HTTP 400. Each of those calls still spends one request of the ~25/day quota (and one of the local
`IPOALERTS_DAILY_LIMIT`), on every cache miss (every 6 h, and after every restart), and logs the same INFO line. The
code only logged it ("IPOALERTS_STATUSES may need trimming per-account"); nothing remembered it.

## Fix
- A 400 whose body says the parameter is "not supported" for the "plan" marks that status as unsupported in the
  durable kv layer for `IPOALERTS_UNSUPPORTED_TTL_S` (default 604800 = 7 days; 0 = old behaviour). It survives a restart.
- Later fetches skip a marked status: no request, no quota spent, and the daily-quota check counts only the statuses
  still asked. If every status is marked, no request is made and the cached list (or `[]`) is returned.
- One INFO line when a status is marked. Any other failure (other 400 text, 429, 5xx, timeout) is NOT marked.
- After 7 days the status is tried once again, so an upgraded plan is picked up by itself.
- `_get_recent_ipos` in main.py is unchanged: it already falls back when the list is short.

## Effect
On the free plan the `listed` rows were never returned anyway, so IPO results do not change; you save one request per
cache miss (about 4 of 25 a day) and one log line.

## Tests
New class `TestIpoalertsPlanRejectsStatus` in `tests/test_ipo_scanner.py` (8 cases: helper, mark with TTL, next fetch skips
and spends 1 not 2, quota check counts only asked statuses, all marked = no request, other 400 not marked, TTL 0 = old
behaviour, kv errors never raise). No pytest in my sandbox: the 8 new cases and the 14 existing `TestFetchIpoalerts`
cases ran through a small stand-in runner with stubbed pytest/httpx and all passed; the full api-gateway suite was NOT run.
On the VM: `cd services/api-gateway && python3 -m pytest tests/test_ipo_scanner.py -q`

## Not changed (log items that need your input or are not code)
See the reply for the full list of issues read from the boot log.
