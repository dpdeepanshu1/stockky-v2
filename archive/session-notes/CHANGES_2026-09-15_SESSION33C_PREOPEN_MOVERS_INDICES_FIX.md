# Session 33c — Pre-open dashboard: empty Market Movers + intermittent indices 503

**Reported:** dashboard screenshot at 09:08 IST (before the 09:15 session
open) showing NIFTY/SENSEX at 0 with `stale:true, fallback:true`, and
"No market data available at the moment" in Market Movers.

## Diagnosis (from live command output)

1. `GET /market/top-gainers` returned `{"data":[],"count":0}` — logs showed
   `"Fetching fresh market movers data from yfinance"` running (200 OK) but
   producing nothing.
2. `_get_nifty50_data()`'s per-symbol fetch (`api-gateway/main.py`) calls
   `ticker.history(period="1d", interval="1m")` — **today's 1-minute
   candles** — for ~150 symbols. Before 09:15 IST no symbol has a 1-minute
   bar for today yet, so every one of those ~150 calls is *guaranteed*
   empty. Because the result (`[]`) is falsy, the existing cache-read guard
   never treats it as a hit, so this expensive ~150-symbol fetch re-ran on
   **every single poll** during the whole 08:30–09:15 "PRE-OPEN"/warm
   window — hammering Yahoo Finance for something that cannot succeed yet.
3. That request volume is the likely reason `get_market_indices()` (NIFTY/
   SENSEX) was intermittently returning `503 Index data temporarily
   unavailable` in the same window even though manually calling
   `yf.Ticker('^NSEI').history(...)` in isolation worked fine — same
   yfinance backend/IP under load from #2, more likely to get throttled on
   any given call. Since this was also right after a fresh restart,
   `INDICES_LAST_KNOWN` had nothing cached yet either, so the 503s surfaced
   as literal `price: 0, stale: true, fallback: true` instead of a graceful
   stale value.
4. The raw unauthenticated `httpx.get("https://www.nseindia.com")` test
   (no headers, no cookies) timed out — but that's not a fair test of the
   app's actual path: `_get_nse_client()` uses full browser headers, a
   bootstrap cookie hop, and a 15s timeout with retry-on-403. Inconclusive
   either way; not touched this session — no evidence it's the cause of
   what's in the screenshot.

## Fix — `services/api-gateway/main.py`, `_get_nifty50_data()`

Reused the market-session-phase helper (`_market_session_phase_ist()`,
already used elsewhere in this file for cache-TTL decisions, and the exact
source of the dashboard's own "PRE-OPEN" label) to skip the guaranteed-empty
1-minute fetch entirely outside the `"open"` phase (09:15–15:30 IST).
Instead:
- If a previous successful day's movers are cached (new
  `MARKET_MOVERS_LAST_KNOWN` key, 7-day TTL, populated on every real
  success), serve that instead of an empty list.
- If there's no last-known data at all yet (e.g. very first run), still
  return `[]` rather than firing the doomed fetch — same outcome the user
  saw, but without hammering Yahoo on every poll.

This does not change anything during actual market hours — the real fetch
path, its lock, and its NIFTY50+NEXT50/MID/SMALL sampling are untouched.

## Not changed this session
- `get_market_indices()`'s own retry/fallback logic — left as-is; the 503s
  should become far less frequent once #2/#3 above stop generating extra
  yfinance load, but this wasn't touched directly since the mechanism
  (last-known fallback) is already sound.
- NSE direct-connectivity question (#4) — inconclusive from the test run;
  flagging for a proper test using `_get_nse_client()`'s actual headers if
  NSE-dependent features (not yfinance-dependent ones) show problems.

## Verification
`python3 -m py_compile` clean on `api-gateway/main.py`; `pyflakes` shows no
new findings beyond pre-existing unrelated ones elsewhere in the file.
