# Round 15 — fix for GET /scan/universe timing out at 20s

## Root cause
`services/api-gateway/main.py`'s `_get_momentum_movers()` fallback path (triggered
whenever NSE's live boards contribute < 40 symbols — likely every run from a
datacenter IP, since NSE frequently 401/403s those) looped over up to 180
symbols and called `yf.Ticker(symbol).history(period="5d", interval="1d")`
**once per symbol, sequentially, in-process**. This:

- caused GET /scan/universe to hang well past the diagnostic script's 20s
  client timeout (unthrottled, the loop can run for minutes)
- bypassed market-data-service's shared yfinance rate limiter / circuit
  breaker entirely, piling an uncoordinated burst of Yahoo calls on top of
  whatever market-data-service was already throttling

## Fix
Replaced the per-symbol loop with a single call to
`bulk_yahoo_download_prices()` (already defined in `data_feed.py` and used
elsewhere in this codebase — e.g. the Hot Picks premarket pre-feed — for
exactly this class of problem). That function makes ONE batched
`POST /quotes/bulk` call to market-data-service, which does ONE
`yf.download()` for the whole list, routed through the rate limiter, with
kv-cache / live-feed short-circuiting for anything already fresh (this is
the same endpoint the diagnostic log showed returning in 3.28s for 10
symbols).

`day_change_pct` from the bulk response drives the same "≥5% day move"
signal as before. The "≥5% week move" leg is dropped — `/quotes/bulk`
only carries today's change, not 5-day history — day change alone remains
the dominant volume-shock signal.

## File changed
- `services/api-gateway/main.py` — `_get_momentum_movers()` fallback block only

## One thing to check before the next log
In the log you sent, port 8008 still returned `000` and `GET
/risk-config/DEMO` still returned `404` — both of those were supposed to be
fixed in round 14 (notification-scheduler Dockerfile port + the new
risk-config route). That usually means the containers were re-run without a
rebuild (`docker compose up` reusing old images rather than
`docker compose up --build`, or `--build` on stale layer cache). Worth a
`docker compose build --no-cache notification-scheduler-service real-trade-service`
(or equivalent) before the next test run, so this round's fix and last
round's fixes are both actually live when you capture the log.
