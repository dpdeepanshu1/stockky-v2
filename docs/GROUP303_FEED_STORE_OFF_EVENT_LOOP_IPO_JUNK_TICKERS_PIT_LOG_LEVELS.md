# group303 (2026-10-10) - feed-store DB calls off the event loop, IPO junk tickers, PIT check, log levels

Cumulative on group302. Rebuild: `docker compose up -d --build api-gateway decision-prediction-service`
(analysis-intelligence-service and market-data-service are unchanged).
Source: the 2026-10-10 12:09 boot log, read together with the group302 notes.

## 1. api-gateway: `event loop blocked for 4.0s` (found by the group302 watchdog)
The stack in the log ended in `oracledb ... commit()` -> `ssl.recv`, right after `repair ABREL: seeded baseline ROCE=15`.
`_patch_single_stock_feed` is `async` and called `store.put_symbol()` directly. One `put_symbol` is a read + three writes + an index
persist (about 5 Oracle round trips), all on the loop, so every request (including /health) waited behind it.
Every `put_symbol` / `get_symbol` call inside an `async def` in `main.py` now goes through `asyncio.to_thread` (14 sites): the
fundamental and events write-throughs, `_analyze_one_symbol_ultra`, the Hot Picks price lookup, `purge_over_cap`, the
refresh-batch writer, repair, `data_feed_run`'s universe check and `_run`'s write. This is very likely the stall group302 could
not reproduce. `tests/test_group303_loop_offload_ipo_filter.py` has an AST guard that fails if a direct store call is added to
an async function again, plus a thread-identity test on the repair path.
Not changed: sync (`def`) routes and `_run_premarket`, which already run in worker threads.

## 2. api-gateway IPO scanner: junk tickers and double Yahoo lookups
- A forced IPO rescan sent 127 symbols to Yahoo (126 `/history` 404s, about 250 yfinance ERROR lines). Sixteen of them are debt or
  unit tickers with no hyphen: STFNCD8, TCFNCD2, SNCD9T2, EHFLNCD, PFCZCB1, ADANIENPP1, CUBEINVIT, UGROD1-3, SMCG01-03, SMC01,
  NMC01, IHFL13. `_is_ipo_non_equity` now matches `...NCD[digits]`, `...ZCB[digits]`, `XXX...PP<digit>`, `...INVIT`, and a short list of
  issuer prefixes followed by 1-2 digits (`UGROD SMCG SMC NMC IHFL`; extend with `IPO_NON_EQUITY_SERIES_PREFIXES=A,B`).
  A generic "letters + digits" rule was rejected because it would drop real SME tickers such as VALUE360. The main equity
  pipeline's `is_non_equity_instrument` is untouched.
- `_fetch_history` now treats a 404 from market-data as the answer (market-data has already run its whole waterfall) and no
  longer falls through to the gateway's own `yf.Ticker(...).history()`. Its docstring already said the direct call was only for an
  unreachable market-data-service. A 5xx or a connection error still falls back.
- Symbols with no history are remembered for `IPO_HISTORY_MISS_TTL_S` (default 10800 s = 3 h, 0 = off, in-process, bounded to 5000),
  so a repeat rescan does not repeat the lookups.
- The remaining ~110 names (EVENTIONS, PAPADMALJI, HIMALAYAN, ...) look like NSE SME listings Yahoo does not carry. That is NOT
  verified; they are now looked up once per 3 h instead of on every rescan.

## 3. decision-prediction-service: the PIT check validated nothing
`POST /training/api/predictions` validated `pred.timestamp`, a field the request model does not have, so it logged
`PIT validation issues ... ['missing_prediction_timestamp']` on every prediction and never ran the clock checks. It now validates the
timestamp that is stored (`ist_now()`, taken once and reused for the row). `ist_now()` is naive IST while the validator's default
clock is `utcnow()`, so `now=` is passed too; without it every row would read as 5.5 h in the future (pinned by a test).
Still warn-only, as before.

## 4. Log levels (decision-prediction-service/training/evaluate.py)
`T+1 skip ... reason=waiting_next_session` and `Not enough data for X on T+5` for a prediction younger than 5 days are normal
states and now log at INFO. Any other T+1 skip reason, and a T+5 gap on a prediction 5+ days old, stay WARNING.

## Tests
New: api-gateway `tests/test_group303_loop_offload_ipo_filter.py` (39), training `tests/test_group303_pit_and_log_levels.py` (5). Both
files were run against the group302 code and fail there (19 failures + 7 errors, and 3 failures).
Updated: api-gateway `tests/test_main_feed_control_audit.py::test_idle_job_is_passed_through_untouched` - it was already failing on
group302 because the status normalisation now also puts `last_count` and `stock_count` into `meta`; the expected dict was stale.

## NOT changed (config or infra, needs you)
- `HF_MODEL=mistralai/Mistral-7B-Instruct-v0.2` is not served to your Hugging Face token (400). I cannot check which models your
  providers serve from here, so no default was guessed. Set `HF_MODEL` in `.env` to one your token can call.
- Gemini 429: free-tier quota; the 10-minute cooldown and fallback already work.
- NSE 403 / weak Akamai cookies: the VM's IP is blocked; bhavcopy already covers it.
- Stale news URLs (Moneycontrol latestnews, NDTV Profit/bloombergquint, CNBC-TV18): the group241 Google News fallback serves them. No
  replacement URL was changed because none can be verified without network access here.

## Check after rebuild
`GET /ops/loop-lag` while running Repair All: `worst_stall` should stay well under the 3 s watchdog threshold.
A forced IPO rescan should log far fewer `possibly delisted` lines, and the second rescan within 3 h almost none.
