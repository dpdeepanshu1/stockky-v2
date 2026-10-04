# group84 (2026-10-04) - items 3 and 15 (AngelOne rate limit / Real Trade fan-out) + log noise

Cumulative on group83. Run `bash run_tests.sh` per service on the VM.

## Items 3 + 15
- market-data `/history`: a shorter period is sliced from an already-cached longer one (1y -> 6mo/3mo/1mo); `force=true` reuses a result fetched in the last `HISTORY_FORCE_REUSE_S` (60) seconds and identical forced requests coalesce (0 = old always-upstream force).
- analysis-intelligence technical: ONE `/history` call (`TECHNICAL_HISTORY_FETCH_PERIOD`, default `1y`, trimmed to the last ~6 months); other periods only after HTTP 404; a 403/429/503/timeout stops the chain (goes to yfinance).
- real-trade-service dashboard prices: `market_feed.feed.get_display_prices` - one shared cache for Positions/Orders/Candidates (`DISPLAY_PRICE_TTL_OPEN_S` 8, `DISPLAY_PRICE_TTL_CLOSED_S` 120, `DISPLAY_PRICE_MISS_TTL_S` 30), single-flight, no ATR `/history` refresh, no `/live-quote` while closed. Trading paths (`get_quote`/`get_quotes`) unchanged.

## Log noise / boot errors
- All 7 services: `httpx`/`httpcore` -> WARNING (`HTTPX_LOG_LEVEL=INFO` restores); successful `/health` access lines dropped (`ACCESS_LOG_HEALTH=1` restores).
- real-trade `oracle_compat.create_index_sql` quotes `mode` (ORA-00936 on 4 indexes). Same file in all 8 copies.
- api-gateway: `stockky:market_movers_last_known` is durable (closed-market Movers panel survives a restart). It still fills only after one OPEN-session fetch.
- `WAKE_PINGS` (api-gateway `_wake_required_services` / `_warm_upstream_services`, technical warm ping): unset -> off when `ORACLE_DSN` is set, on otherwise; `1`/`0` forces.
- position-stocks WS idles outside 08:55-15:45 IST weekdays (`POSITION_WS_OFFHOURS_IDLE=0` restores always-connect).
- news: Moneycontrol and Financial Express removed (HTTP 403 from the VM); Google "0 entries" logged at INFO.

## Not changed
Boot-time AngelOne movers sweep (the after-hours scan uses it right after boot), items 17/22/26, Yahoo news (returns 0 for every symbol).
