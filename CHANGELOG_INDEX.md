# Changelog index

Per-session fix notes live in `archive/session-notes/` (one file per
session, dated). This file is just an index so they're discoverable
without 50+ files cluttering the repo root.

Most recent first — see each file for full detail:

- 2026-10-08 (group 260)  Charges since start now includes SELLs (real-trade-service execution/reconcile.py, charges_ledger.py, frontend PositionStocksTab.tsx).
  Exit fills never wrote `trade_fills`, so the cumulative report skipped every sell (STT / DP read Rs 0, total Rs 55.95 vs Rs 93.06 today). SELL fills are now recorded; pre-fix sells are priced from broker_fill_notional or filled qty x limit price. Position Stocks "Net" now subtracts today's ledger charges instead of the BUY-only live snapshot. 10 new tests; real-trade suite 3754 passed; tsc clean. See docs/GROUP260_SELL_FILLS_IN_CHARGES.md.
- 2026-10-08 (group 258)  Both Charges tabs show brokerage since the start (position-stocks-service orders/charges_ledger.py + new scalp_charges_ledger table, real-trade-service charges_ledger.py, frontend PositionStocksTab / RealAutoTrade).
  Position-stocks deletes closed trades after 3 days, so each settled trade is booked once into a never-purged ledger table; real-trade sums its never-purged orders/fills. New `GET /charges/cumulative` and `GET /charges/{mode}/cumulative`; env `CHARGES_BROKERAGE_PCT` (0.03), `CHARGES_BROKERAGE_CAP_RS` (20), `CHARGES_DELIVERY_BROKERAGE_RS` (0). Measurement only, no gate changed. 15 new tests, real pytest; tsc clean. Not run against a live DB. See docs/GROUP258_CUMULATIVE_BROKERAGE.md.
- 2026-10-08 (group 257)  Two misleading log lines fixed: the AngelOne quote-cooldown miss line no longer says "using the Yahoo path", and the regime-WEAK line names the top-N override (market-data-service main.py, real-trade-service entry_engine/entry.py).
  The 09:42 IST BUY after "regime WEAK ... BUYs blocked" was the intended `ENTRY_REGIME_OVERRIDE_TOP_N` (1) override at 50% risk, not a leak. Log text only, no behaviour change. See docs/GROUP257_COOLDOWN_MISS_LOG_TEXT.md.
- 2026-10-08 (group 256)  While AngelOne's quote cooldown runs, unheld symbols get a recent cached price (or a quick "no price") instead of the Yahoo path; candle recovery is measured by send time (market-data-service main.py / angelone_budget.py / angelone_client.py, real-trade-service market_feed/feed.py).
  14:15 IST log: a quote 403 sent hundreds of symbols to a saturated yfinance (18 s timeout, ~100 ReadTimeouts); the "answered again, 1s after the first 403" recovery line was a call already in flight. New env `QUOTE_COOLDOWN_SERVE_STALE` (1), `QUOTE_COOLDOWN_SKIP_YAHOO` (1), `QUOTE_COOLDOWN_STALE_MAX_AGE_S` (180). Held symbols unchanged. The AngelOne 403 warning line now also shows response headers (Retry-After, Server, Via ...) and session age. See docs/GROUP256_RECOVERY_INFLIGHT_AND_COOLDOWN_STALE.md.
- 2026-10-08 (group 255)  Candle cooldowns climb 30, 60, 120, 240 ... up to 600 s, and the budget logs how long AngelOne's candle block lasted (market-data-service angelone_budget.py / angelone_client.py).
  Group 254 counters in the 08:36 IST boot log: only 3 real candle calls since boot, 2 answered 403, the second one 0.0 s after the 30 s cooldown ended, quote rate about 1.6/s with no quote trip, `throttle_events 0`. Our rate is not the cause; AngelOne's block outlasts a short cooldown. New `ANGELONE_CANDLE_COOLDOWN_MAX_S` (default 600; 60 = old ceiling, quote cooldowns unchanged), new INFO line `candle calls are answered again, Ns after the first 403 of this run`, and `candle_calls.last_block_lasted_s` in `/angelone/budget`. 14 new tests (sandbox stand-in runner; run `bash run_tests.sh` on the VM). See docs/GROUP255_CANDLE_LADDER_AND_RECOVERY.md.
- 2026-10-08 (group 254)  Candle 403 diagnostics: every real getCandleData and quote send is counted and each candle trip logs what was sent just before it (market-data-service angelone_budget.py / angelone_client.py).
  Pre-market log + `/angelone/budget`: candle trips #1 and #2 within about a minute of boot with the group 251 slowdown active (effective 0.5/s), `throttle_events 0`, `waiters 0` and only about 10-20 real candle calls a minute (documented limit 3/s, 180/min), so our own rate does not explain it. New `candle 403 context` WARNING line (candle and quote calls in the last 10 s / 60 s, seconds since the first call after the previous cooldown) and `candle_calls` / `quote_calls` in `GET /angelone/budget`. Diagnostics only; no limits, cooldowns or routing changed. 17 new tests (sandbox stand-in runner; run `bash run_tests.sh` on the VM). See docs/GROUP254_CANDLE_TRIP_DIAGNOSTICS.md.
- 2026-10-08 (group 253)  The dynamic universe's /check warm-up runs on its own thread instead of holding the cycle for 90 s (real-trade-service watchlist_engine/dynamic_universe.py).
  Log: `/check trigger failed (ReadTimeout)` about 90 s after `added 60 symbols`. The event service's `/check` walks every subscription serially (1 s stagger + a fresh fetch each), which takes minutes, so the 90 s wait timed out on every sync and stalled the dynamic-universe -> watchlist stage for that long. Now started on a daemon thread (cycles run on throw-away event loops, which would kill a background task), one at a time, `DYNAMIC_UNIVERSE_CHECK_TIMEOUT_S` 600, real outcome and elapsed time logged; `DYNAMIC_UNIVERSE_CHECK_BACKGROUND=0` = old behaviour. 24 new tests, real pytest (3724 passed). Not confirmed live. See docs/GROUP253_CHECK_TRIGGER_BACKGROUND.md.
- 2026-10-08 (group 252)  Held-position prices are hedged: per-symbol lookups start after 1.5 s if the priority bulk call has not answered (real-trade-service market_feed/feed.py).
  Log: the 6-symbol held bulk chunk timed out after its full 4 s and only then did the per-symbol path start (all answered 200 immediately), so held prices and the 8 s exit cycle lost 4 s. Smaller-chunk retry cannot help a single 6-symbol chunk, so `_priority_hedge` runs the per-symbol cascade alongside a slow bulk (`FEED_PRIORITY_HEDGE_S` 1.5, 0 = old); each symbol takes the first answer; an unanswered bulk still pauses bulk-first 30 s. 12 new tests, real pytest (3701 passed). Not confirmed live. See docs/GROUP252_PRIORITY_BULK_HEDGE.md.
- 2026-10-08 (group 251)  After an AngelOne candle 403 the candle bucket is held empty for the cooldown and then refilled at half rate, instead of bursting (market-data-service rate_limiter.py, angelone_budget.py).
  Log (open): candle trip #1 then #2 within minutes. During the 30 s cooldown the candle bucket refilled to full and the queued `/history` callers all fired when it ended. New `rate_limiter.slow_down()`; a candle trip empties the bucket, holds the refill until the cooldown ends, then refills at `ANGELONE_CANDLE_SLOWDOWN_FACTOR` (0.5) for `ANGELONE_CANDLE_SLOWDOWN_S` (600) s; candle bucket default 1.5/3 -> 1.0/2. Quote buckets untouched. 20 new tests, real pytest (market-data 1204 passed). Rates are judgement calls, not measured. Not confirmed live. See docs/GROUP251_CANDLE_POST_TRIP_SLOWDOWN.md.
- 2026-10-08 (group 250)  "momentum-movers source failed ()" now names the exception and elapsed time, and its client waits 45 s instead of 15 s (real-trade-service watchlist_engine/dynamic_universe.py).
  Log: the dynamic universe's warning was empty because an httpx timeout stringifies to "", and 15 s is shorter than api-gateway's cold momentum-movers computation (followers may wait up to 45 s). New `_err_text` used for the movers, volume-shock, subscribe, unsubscribe and /check warnings; movers timeout is `DYNAMIC_UNIVERSE_MOVERS_TIMEOUT_S` (default 45, blank-safe). Failure stays non-fatal. 16 new tests, real pytest (3689 passed). Not confirmed live. See docs/GROUP250_MOVERS_ERROR_TEXT_TIMEOUT.md.
- 2026-10-08 (group 249)  Watchlist symbols that bulk could not price reuse their last good tick instead of per-symbol calls (real-trade-service market_feed/feed.py, entry_engine/entry.py).
  Log (after group 248): the retry priced 168 of 300 lost symbols, but the 138 still unpriced went to 120 per-symbol `GET /quote` calls (AngelOne quote lane in cooldown, yfinance saturated) and ~100 ended in `source-2 failed: ReadTimeout`. Now `get_quotes(..., allow_stale=True)` - used ONLY by the watchlist trigger, which merely queues a candidate - serves such a symbol from the last tick the non-priority path priced for it if it is at most `FEED_LEFTOVER_STALE_S` (default 120 s, 0 = off) old; the Tick keeps its real `as_of` and is tagged `stale_last_good(...)`. Only symbols with no recent tick still go per-symbol. Entry, order-fill, exit and display callers are unchanged. 8 new tests + 10 test doubles in test_watchlist_trigger.py accept the new keyword, real pytest. Not confirmed live. See docs/GROUP249_LEFTOVER_LAST_GOOD_TICK.md.
- 2026-10-08 (group 248)  A /quotes/bulk chunk that times out is asked again in smaller bulk calls before the per-symbol path (real-trade-service market_feed/feed.py).
  Log (10:16 IST): 1 of 8 bulk chunks of a 709-symbol poll timed out, its 100 symbols fell to 120 per-symbol `GET /quote` calls, AngelOne's lane budget shed them to the saturated Yahoo path and ~15 ReadTimeouts followed. Failed-chunk symbols are now retried as 25-symbol bulk calls (`FEED_BULK_RETRY_CHUNK_SIZE`, 20s `FEED_BULK_RETRY_TIMEOUT_S`); only when at least one chunk answered; `FEED_BULK_RETRY_FAILED=0` = old. 6 new tests, real pytest. Not confirmed live. See docs/GROUP248_FAILED_BULK_CHUNK_RETRY.md.
- 2026-10-08 (group 247)  Candidate 6-month and 1-year views are cut from the daily 1y series instead of two weekly yfinance calls (real-trade-service candidate_engine/candidates.py).
  Log: 8 real candidates skipped as "cannot judge" and 2 history ReadTimeouts because the yfinance bucket was saturated (4 of the 7 /history calls per candidate go there). Return and 52-week high/low are the same from daily as from weekly bars, so yfinance calls per candidate drop 4 -> 2; falls back to the weekly calls if the daily series is missing/short (`CANDIDATE_WEEKLY_FROM_DAILY=0` = old). 8 new tests, real pytest. Not confirmed live. See docs/GROUP247_WEEKLY_VIEWS_FROM_DAILY.md.
- 2026-10-08 (group 246)  Candidate/entry cycle no longer floods market-data while held positions are priced (real-trade-service candidate_engine/candidates.py, market_feed/feed.py).
  Log: the 6 REAL positions hit bulk-chunk ReadTimeouts three times while the 3-minute cycle sent ~80 `GET /quote`, ~100 in-flight `/history` and ~40 per-symbol entry quotes. Now the bulk prefetch's quotes are reused (no per-symbol `/quote` for priced symbols; `CANDIDATE_USE_BULK_QUOTES=0` = old), candidate `/history` calls are capped in flight (`CANDIDATE_HISTORY_MAX_INFLIGHT`, default 6, 0 = off), and non-priority batches of 5+ symbols price bulk-first (`FEED_SMALL_BATCH_BULK_MIN_SYMBOLS`, default 5). 17 new tests + 2 updated, real pytest. Not confirmed live. See docs/GROUP246_BULK_QUOTES_HISTORY_PACING_SMALL_BATCH_BULK.md.
- 2026-10-08 (group 245)  DB-warm ATR refreshes after a restart are rate-limited, which was the real source of the AngelOne candle 403 burst (real-trade-service market_feed/feed.py).
  Log: 13 `/history?period=1mo` calls in one second, then getCandleData 403 and 30-60 s candle cooldowns. After a restart `_ATR_LAST_OK` is empty while the ATR cache is warm from the DB, so every priced symbol looked due for a refresh (each an AngelOne 1y candle call). Now at most `FEED_ATR_WARM_REFRESH_PER_MIN` (default 6, 0 = old behaviour) such refreshes per minute; symbols with no ATR are never limited. Corrects the earlier note that the stock page's 7 /history calls caused it (group 231 already collapses those). 6 new tests, real pytest. See docs/GROUP245_WARM_ATR_REFRESH_THROTTLE.md.
- 2026-10-08 (group 244)  pre-open /quotes/bulk serves the last close for symbols AngelOne could not price instead of calling yfinance (market-data-service main.py, market_hours.py).
  Log: one bulk call priced 50/88 via AngelOne REST, left 38 for yf.download, which hit the 18 s hard timeout (ERROR + 502). New `is_preopen_ist()` (09:05-09:15 IST trading days) and `_quote_preopen()`; in pre-open the unpriced leftovers use the group 233 last-close helper (original fetched_at kept); names with no close and index symbols still go to yfinance; `QUOTE_PREOPEN_SERVE_LAST_CLOSE=0` turns it off. Does not cover the same pattern after 09:15. 11 new tests, full suite 1184 passed. See docs/GROUP244_PREOPEN_BULK_LAST_CLOSE.md.
- 2026-10-08 (group 243)  dashboard prices (Candidates / Positions / Orders) priced with one chunked POST /quotes/bulk instead of GET /live-quote + GET /quote per symbol (real-trade-service market_feed/feed.py).
  The 2026-10-08 open log showed ~50 per-symbol live-quote + quote calls after `GET /candidates/REAL?limit=40`, then 20+ AngelOne "lane budget shed" lines and Yahoo fallbacks. `get_display_prices` now bulk-first for 8+ misses (age limit `FEED_DISPLAY_BULK_MAX_AGE_S` 60 s open / preview limit closed); leftovers skip /live-quote and are capped at `FEED_DISPLAY_LEFTOVER_MAX` 10 per poll (uncapped ones retried next poll); all-chunks-failed keeps the old full path; `_bulk_ticks(schedule_atr=False)` for display; `FEED_DISPLAY_BULK=0` turns it off. Trading paths untouched. 11 new tests, real pytest. See docs/GROUP243_DISPLAY_PRICES_BULK_FIRST.md.
- 2026-10-08 (group 242)  Surprise page stream prices its universe with chunked POST /quotes/bulk instead of one GET /quote per symbol (api-gateway surprise_scanner.py, main.py).
  The 2026-10-08 pre-open log showed hundreds of per-symbol /quote calls each time the Surprise page opened and only one bulk call: group 229's prefetch was in scan() but not in the /api/surprise/scan/stream loop. New `prime_bulk_ticks` (fresh dict, then swapped in; stale ticks for those symbols dropped), `_prefetch_bulk(store=)`; stream primes once before its chunks, falls back to per-symbol on failure; `SURPRISE_BULK_PREFETCH=0` turns it off. SMCG04, HF_MODEL, Yahoo news, NSE bootstrap 403 not changed. Engine tests run under a stand-in runner, route tests unrun (no fastapi). See docs/GROUP242_SURPRISE_STREAM_BULK_PRIME.md.
- 2026-10-08 (group 241)  frozen RSS feeds treated as unavailable; CNBC TV18 (404) and NDTV Profit (frozen) fallbacks (real-trade-service afterhours_scan.py, analysis-intelligence-service news/feed_fetch.py).
  Boot log: Moneycontrol 15/15 and NDTVProfit 20/20 items stale, CNBC feed 404. All-stale 200s now try the fallback URLs (env `NEWS_FEED_STALE_DAYS`, default 3); Google News site-search fallbacks with publisher suffix stripped. HF_MODEL, SMCG04, Yahoo news, NSE bootstrap 403 not changed. See docs/GROUP241_STALE_FEEDS_AND_DEAD_CNBC.md.
- 2026-10-08 (group 240)  event service site feeds (Moneycontrol/ET/CNBC TV18) use news/feed_fetch.py instead of bare feedparser.parse (analysis-intelligence-service).
  `_site_feed_parse` loads the shared httpx downloader by file path (browser headers, 403 retry, Moneycontrol fallbacks, timeout); `EVENT_FEED_SHARED_FETCH=0` restores the old call; tests/conftest.py keeps existing event tests on feedparser. See docs/GROUP240_EVENT_SERVICE_SHARED_FEED_FETCH.md.
- 2026-10-08 (group 239)  Moneycontrol HTTP 403 in the news pillar (analysis-intelligence-service news/feed_fetch.py).
  Header-profile retry on bot-gate statuses, fallback URLs for Moneycontrol (business.xml, Google News site search with suffix stripped), blocked feed remembered for `NEWS_FEED_BLOCKED_TTL_SEC` (1800) instead of 60 s, `NEWS_FEED_FALLBACKS=0` to disable. Fallbacks not live-verified; event/main.py still uses feedparser directly. See docs/GROUP239_NEWS_PILLAR_MONEYCONTROL_403.md.
- 2026-10-08 (group 238)  Moneycontrol RSS HTTP 403 in the after-hours/intraday news scan (real-trade-service).
  `_fetch_rss_items` retries a bot-gate status once with an alternate header profile, falls back through `fallback_urls` (Moneycontrol: business.xml, then a Google News site search with the " - Moneycontrol" suffix stripped), and skips a fully-blocked feed for `AFTERHOURS_RSS_BLOCKED_COOLDOWN_SECONDS` (default 1800). Fallback URLs not live-verified. See docs/GROUP238_MONEYCONTROL_RSS_403_FALLBACK.md.
- 2026-10-08 (group 237)  stored admin token is checked once before the first protected REAL request (real-trade-service, frontend).
  The 2026-10-07 log had /pipeline/status/REAL, /watchlist-entries/REAL and /resilience/status each answer 401 twice on page load: `loggedIn` starts as "a token is in localStorage", and an expired token went out with every poller that starts together. New `GET /auth/session` (always 200, `{"valid": bool}`, same decode as the admin routes). `rtRequest` makes one `/auth/session` call first, other callers wait for it; an expired token is cleared, the expiry banner fires once and the protected request is not sent. Fails open (old server, network error, non-200). Real-trade suite here: 3593 passed, 1 skipped, 1 error (group172 teardown, same on the unmodified upload); frontend `tsc --noEmit` clean, not browser-tested. Not done: position-stocks `/dhan/account` single 401 before login, real-trade per-symbol /quote after bulk, Moneycontrol 403, HF_MODEL, AAKASH delisted warning each cycle. Doc: docs/GROUP237_SESSION_CHECK_BEFORE_PROTECTED_POLLS.md. Tests: real-trade tests/test_group237_auth_session.py (8).
- 2026-10-08 (group 236)  closed market: the quote waterfall leaves out the quota-limited sources (market-data-service).
  The 2026-10-07 log had QUALIANCE and BMISL (in no bhavcopy, nothing at Yahoo) walk NSE-direct -> AngelOne -> Yahoo -> IndianAPI -> TwelveData -> AlphaVantage -> Polygon after hours and trip the IndianAPI (429, +120 s) and AlphaVantage (429, +300 s) cooldowns. New `_quote_closed_skip_quota_sources()`; while the market is closed `/quote/{symbol}` skips IndianAPI, TwelveData, AlphaVantage and Polygon (NSE-direct, AngelOne and Yahoo still run). `QUOTE_CLOSED_SKIP_QUOTA_SOURCES=0` restores the old walk; open market and index symbols unchanged. Full market-data suite here: 1173 passed. Not done: real-trade per-symbol /quote after bulk, 401 polls before login, Moneycontrol 403, HF_MODEL, AAKASH delisted warning each cycle. Doc: docs/GROUP236_CLOSED_MARKET_SKIP_QUOTA_SOURCES.md. Tests: market-data tests/test_group235_closed_skip_quota_sources.py (7).
- 2026-10-08 (group 235)  closed-market bhavcopy quotes carry the real day change; bhavcopy hit log flood (market-data-service).
  From the 2026-10-07 evening log after group 234: prewarm 2941/2941 with a close, no AngelOne shed / Yahoo for bhavcopy symbols, REAL cycle 409 then force 200, COHANCE reconcile OK. New: `_closed_last_close_row` built bhavcopy rows with previous_close == close, so every closed-market quote read "0.0% today" (229 volume-shock candidates rejected on "Today's return 0.0%"). `_parse_bhav_csv_all` now returns prev_close / day_high / day_low / volume, new `eod_row_from_bhavcopy()` (eod_close_from_bhavcopy delegates), and the closed row gets previous_close, day_change_pct, day_high, day_low, volume from the same bhavcopy line (only when its close equals the price used). "Bhavcopy EOD waterfall hit" is INFO once per symbol and price (was ~1,500 lines in minutes). Full market-data suite here: 1166 passed. Not done: unlisted symbols (QUALIANCE, BMISL) still walk Yahoo + IndianAPI/AlphaVantage (429), real-trade per-symbol /quote after bulk, 401 polls before login, Moneycontrol 403, HF_MODEL, AAKASH delisted warning each cycle. Doc: docs/GROUP235_CLOSED_QUOTE_DAY_CHANGE_LOG_NOISE.md. Tests: market-data tests/test_group235_closed_quote_day_change.py (10).
- 2026-10-07 (group 234)  bhavcopy close price actually parsed, manual REAL cycle refused off-hours with stage deadlines, IndianAPI only on a real "no data" answer (market-data-service, real-trade-service, analysis-intelligence-service, frontend).
  (1) Root cause of group 233 not working: NSE's `sec_bhavdata_full` names the close column `CLOSE_PRICE` / `LAST_PRICE`, the parsers only knew `ClosePrice`, so every bhavcopy row had `close=None` and `eod_close_from_bhavcopy()` found no symbol; new `_CLOSE_COL_NAMES`, one WARNING per header with no close column, single-flight day download, boot prewarm (`BHAVCOPY_PREWARM`, `BHAVCOPY_DAY_WAIT_S`). (2) `POST /cycle/run/REAL` returns 409 outside market hours unless `?force=true` (`MANUAL_REAL_CYCLE_OFFHOURS_BLOCK=0` = old behaviour); a manual cycle's watchlist and candidates stages stop after `CYCLE_MANUAL_STAGE_TIMEOUT_S` (60) and the result carries `timed_out` / `stage_timeouts`; frontend confirms and retries with force, shows partial results, and reports which path failed instead of "Failed to fetch". (3) `/analyze/{symbol}` skips IndianAPI when market-data timed out or answered 429/5xx (`INDIANAPI_ON_MD_FAILURE=1` = old behaviour). Full suites here (real pytest): market-data 1156 passed; analysis-intelligence 2400 passed, 1 failed (test_event_main route-order quirk, identical on the unmodified upload); real-trade 3585 passed, 1 skipped, 1 error (group172 teardown, same on the unmodified upload); frontend `tsc --noEmit` clean, not browser-tested. Not done: concurrency cap on heavy market-data routes, AngelOne lane budget, HF_MODEL, Moneycontrol 403, COHANCE snapshot, protected-endpoint polling before login. Doc: docs/GROUP234_BHAVCOPY_CLOSE_PRICE_OFFHOURS_CYCLE_INDIANAPI.md. Tests: market-data tests/test_group234_bhavcopy_close_price.py (10); real-trade tests/test_cycle_runner.py (+6), tests/test_main_routes_trading.py (+6); analysis-intelligence tests/test_fundamental_main.py (+9).
- 2026-10-07 (group 233)  bulk quotes instead of one call per symbol, last close served when the market is closed (market-data-service, api-gateway, real-trade-service). Doc: docs/GROUP233_BULK_QUOTES_CLOSED_MARKET_LAST_CLOSE.md
  market-data `/quote` and `/quotes/bulk` answer a closed market from the cached / last-good / bhavcopy close without touching AngelOne or Yahoo (`QUOTE_CLOSED_SERVE_LAST_CLOSE`); gateway Hot Picks price pass, `/market/trending` and the hot-picks price repair use chunked `POST /quotes/bulk` (no per-symbol `/quote`); real-trade `get_preview_quotes` is bulk -> `/last-close` -> at most 5 `/quote` (`FEED_PREVIEW_QUOTE_FALLBACK_MAX`).
- 2026-10-07 (group 232)  sold-today holdings netted against live positions (COHANCE phantom OPEN), closed cards use the real BUY fill from the order book (real-trade-service, position-stocks-service).
  Items 1 and 3. (1) `holdings_sync_reconcile` subtracts a negative `netQty` from a holding Dhan still lists until settlement, so a position sold at the broker is closed (or capped) instead of staying OPEN; event and log say why. (2) When the super-order row has no entry price, the BUY fill comes from today's order book (own security id, TRADED, exact qty, within 5%, one clear match); used live and in the repair, skipped for rejected entries; `auto_repair_closed_entry_prices` re-runs the repair every `ENTRY_REPAIR_AUTO_INTERVAL_S` (300, 0 = off). INDORAMA and the missing ICIL/INOXINDIA/DOMS cards need `trade_orders` rows. Doc: docs/GROUP232_COHANCE_NET_SELLS_CARD_FILL_PRICES.md. Tests: real-trade test_group232_holdings_net_sells.py (10), position-stocks test_group232_entry_fill_orderbook.py (23).
- 2026-10-07 (group 231)  held-position probe during a quote cooldown, one 1y candle call per symbol, Tier 1 watchlist guard (market-data-service, real-trade-service).
  Items 3, 1 and 5 of the 14:57 IST log review. (1) During a quote-family 403 cooldown a held-position (POSITION lane) call may send one probe per second (`ANGELONE_POSITION_PROBE_INTERVAL_S`, 0 = off); a probe answered 403 never extends the cooldown and pauses probing 10 s; other lanes and the candle family stay silent; `/angelone/budget` shows `position_probes`. (2) `/history` answers `5d`/`1mo`/`3mo`/`6mo` (1d, no `days`) from ONE AngelOne `1y/1d` fetch cached under the 1y key and sliced, instead of up to four candle calls per symbol; a failed 1y call is not repeated; last-good 1y also serves short periods; `HISTORY_WIDEN_DAILY=0` turns it off. (3) Tier 1 watchlist rows: drop limit 1.5% (`WATCHLIST_TIER1_MAX_DROP_PCT`) and not down on the day (`WATCHLIST_TIER1_MIN_DAY_CHANGE_PCT`, default 0.0, `off`); Tier 2/3 unchanged; unknown day change still passes. Full suites: market-data 1134 passed; real-trade 3556 passed, 1 skipped, 1 error (group172 teardown, same on the unmodified upload). Fixed three old test failures found on the way (group164, history_reuse, group191). Doc: docs/GROUP231_POSITION_PROBE_HISTORY_WIDEN_TIER1_GUARD.md. Tests: market-data tests/test_group231_position_probe.py (16), tests/test_group231_history_widen.py (11); real-trade tests/test_group231_tier1_guard.py (8).
- 2026-10-07 (group 230)  history cache + last-good candles, feed batch concurrency, Tier 2/3 previous-close hold, exit-tick timing warning, gate log note (market-data-service, real-trade-service). Doc: docs/GROUP230_LOG_REVIEW_HISTORY_FEED_ENTRY_NOTES.md.
- 2026-10-07 (group 229)  Surprise scan prices the universe with chunked POST /quotes/bulk first (item 8 of the 14:02 IST log review) (api-gateway).
  Item 8. The per-symbol `/quote` storm from 172.18.0.7 (about 600 symbols one by one every few minutes, hundreds of `AngelOne-first did not price X (lane budget shed this call)` lines, each falling to Yahoo) comes from `surprise_scanner.py` `scan()`, which sent one `GET /quote/{sym}` per liquid-universe symbol whenever the shared bulk cache was cold. `scan()` now calls `_prefetch_bulk()` for the whole key list (and again for the sector-sympathy peers): chunked `POST /quotes/bulk` (100 per chunk, 2 chunks at a time, 15 s timeout), rows older than 30 s ignored, results kept in a per-scan `_bulk_ticks` map that `_fetch_quote` reads after the gateway's own bulk cache and before the per-symbol call. Only symbols bulk could not price still use `/quote/{sym}`. Never raises; a failing chunk just leaves its symbols for the old path. Env: `SURPRISE_BULK_PREFETCH=0` turns it off, `SURPRISE_BULK_CHUNK`, `SURPRISE_BULK_TIMEOUT`, `SURPRISE_BULK_CONCURRENCY`, `SURPRISE_BULK_MAX_AGE_SEC`. Not changed: `_fetch_prices_bulk_async` and the repair/premarket per-symbol loops (they only run on misses). Tests: api-gateway tests/test_group229_surprise_bulk_prefetch.py (new, 18, pass under a stand-in runner); tests/test_surprise_scanner.py ScanRig now stubs `_prefetch_bulk` (2 chunk-size tests unaffected). Real pytest not available in the sandbox: the scanner suite shows the same 69 stand-in/no-sqlalchemy failures before and after the change. Doc: docs/GROUP229_SURPRISE_BULK_PREFETCH.md.
- 2026-10-07 (group 228)  AngelOne cooldown split into a candle family and a quote family (item 1 of the 10:33 IST log review) (market-data-service).
  Item 1. In the 13:39 IST boot log a `getCandleData` 403 started the single group 211 cooldown, and every quote caller then logged `AngelOne-first did not price X (global AngelOne cooldown (403) is running)` while quotes had been answering normally (`AngelOne REST resolved 89/89`) right before. The feed stopped after 52 of 499 tokens, `/quote` fell to yfinance (18 s hard timeouts, "rate-limit bucket saturated"), only 306 of 653 symbols were priced and about 50 real-trade `/quote` calls ReadTimed out. `angelone_budget.py` now keeps the cooldown per family: `candle` (getCandleData) and `quote` (ltpData, quote batches, feed poll, gainers). A candle 403 pauses candle callers only, a quote 403 pauses quote callers only; each family escalates (30 s -> 60 s) and suppresses late answers on its own. `angelone_client.py` (`get_candles` checks the candle family, the rest the quote family) and `angelone_ws_feed.py` (poll cycle, mid-cycle stop and the HTTP-denied safety net use the quote family). `ANGELONE_SPLIT_COOLDOWN=0` restores the shared cooldown. `GET /angelone/budget` gains `split_cooldown` and per-family `cooldowns` (old keys kept; they now mean "any family"). Updated tests that asserted the old behaviour (group 211: 6, group 225: 1). Items 3 and 4 of the review are still open. Market-data suite here: 1079 passed. Doc: docs/GROUP228_SPLIT_ANGELONE_COOLDOWN.md. Tests: market-data tests/test_group227_split_cooldown.py (new, 34).
- 2026-10-07 (group 227)  Rejection cache for the standard candidate track (item 2 of the 10:33 IST log review) and the missing `5d` period in market-data (real-trade-service, market-data-service).
  Item 2. The same symbols (AXISCADES, RAYMOND, TRANSRAIL, JAYKAY, INDHOTEL, AETHER, KAVDEFENCE, DELTACORP, NYKAA ...) were rejected for stable reasons ("top 12% of 52w range", "6m return < -10%") and re-evaluated every 3 minutes at 7 `/history` calls + a quote + a market-cap call each - the biggest candle-load driver behind the AngelOne 403 cooldown. `candidate_engine/candidates.py`: `_multi_tf_analysis` tags every definite rejection with `reject_kind`; a per-symbol cache remembers them (`REJECT_CACHE_STABLE_S`=3600 for price floor / 6m downtrend / ATR cap / volume health, `REJECT_CACHE_PRICE_S`=900 for weighted bullish score / 52w range / near resistance; both 0 = off). Cached symbols skip the quote prefetch, all 7 history calls and the market-cap call, and log at DEBUG; one INFO line per cycle counts them. Never cached: data-starved, incomplete-history, no-quote, errors, passes; entries are dropped at the IST date change and when the adaptive ATR cap rises above the cached ATR. Also found while tracing: `5d` was missing from market-data's AngelOne and NSE period maps, so the candidate engine's "1w" request silently pulled 180 days whenever AngelOne served the candles (a 6-month return labelled 1 week); it is now 7 days and `5d` is never widened by `MAX_HISTORY_PERIOD`. Items 1 and 3 of the same review are NOT done. Real-trade suite here: 3533 passed, 1 skipped, 1 error (the group172 teardown error, identical on the unmodified upload); market-data 1045 passed. Doc: docs/GROUP227_REJECTION_CACHE_AND_5D_PERIOD.md. Tests: real-trade tests/test_group227_reject_cache.py (new, 24), market-data tests/test_group227_5d_period_window.py (new, 4).
- 2026-10-07 (group 226)  Hot Picks warm-up re-poll (item 5), a correction to group 225, and a "why no trades" report (real-trade-service).
  Item 5: api-gateway answers /stockky-hot with empty buckets and `"warming": true` right after a boot; Tier 1 took that for "no catalysts" and the first watchlist refresh ran on Tier 2/3. `watchlist_engine/sources.py` now re-polls it (`WATCHLIST_TIER1_WARMUP_RETRIES`=3 x `WATCHLIST_TIER1_WARMUP_WAIT_S`=8, at most once per `WATCHLIST_TIER1_WARMUP_MIN_GAP_S`=120; 0 retries = off). Correction to group 225: its back-pressure paused per-symbol lookups for every non-priority batch, including the <=20 entry candidates, so an unpriceable held symbol could leave every candidate with "No current price"; it now pauses only large (watchlist-poll) batches and only when a held symbol is still unpriced after every attempt. New read-only `scripts/no_trade_diagnosis.py` (gate state, candidates, ENTRY/EXIT decisions grouped by reason, orders, positions). Doc: docs/GROUP226_TIER1_WARMUP_NO_TRADE_DIAGNOSIS.md. Tests: real-trade tests/test_group226_tier1_hot_picks_warmup.py (new, 8), tests/test_no_trade_diagnosis.py (new, 5), group225 tests +2.
- 2026-10-07 (group 225)  open positions keep a price under load (real-trade-service) and the AngelOne feed backs off after a 403 (market-data-service).
  Items 1 and 3 of the open-market log review. At 09:43 the six REAL positions timed out on bulk, /live-quote and /quote every exit cycle while the watchlist poll sent ~579 per-symbol lookups; the AngelOne feed logged "quote batch failed" every ~3 s. `market_feed/feed.py`: when the whole priority lane cannot price a held symbol it returns the last tick it priced (at most `FEED_PRIORITY_STALE_FALLBACK_S`=90 s old, real `as_of`, source `stale_last_good(...)`, 0 = off); a lane failure switches non-priority per-symbol leftovers off for `FEED_BACKPRESSURE_S`=20 and one batch sends at most `FEED_LEFTOVER_MAX`=120 of them (0 = no cap). `angelone_client.py`: `_is_rate_limit_response` also reads the raw body (the gateway 403 is plain text, so no cooldown ever started); `angelone_ws_feed.py`: the cycle stops when the shared cooldown starts mid-cycle and any batch HTTP 403/429 trips it. Test hygiene: the 4 group171 failures on the group 224 upload (module-reload `Tick` identity) are fixed. Full suites run here: market-data 1041 passed; real-trade 3494 passed, 1 skipped, 1 error (the group172 teardown error, identical on the unmodified upload). Doc: docs/GROUP225_PRIORITY_LANE_FALLBACK_AND_ANGELONE_403_BACKOFF.md. Tests: real-trade tests/test_group225_priority_lane_backpressure.py (new, 13), market-data tests/test_group225_plain_text_403.py (new, 10).
- 2026-10-07 (group 224)  a transient /history failure reuses the last good daily candles (real-trade-service).
  Item A3 of the open-market log review. At 09:15-09:25 market-data /history timed out for 25 of 157 volume-shock symbols (large caps such as DIVISLAB, NESTLEIND, DRREDDY) and 15 main-track symbols, each skipped for the cycle as "cannot judge". `_fetch_history` now keeps the last good 1d/1wk/1mo answer per symbol/period and, on a timeout / 403 / 429 / 5xx only, returns it if younger than `CANDIDATE_HISTORY_STALE_FALLBACK_S` (default 1800, 0 = off). Empty answers, 404/400 and short history stay definite (no fallback, same 6 h pause); the failure reason is still recorded. Memory only (nothing to reuse right after a restart). Full real-trade suite run here: 3477 passed; the 4 failures + 1 error present are identical on the unmodified group 223 upload. Doc: docs/GROUP224_HISTORY_STALE_FALLBACK.md. Tests: real-trade-service tests/test_group224_history_stale_fallback.py (new, 11).
- 2026-10-07 (group 223)  early pre-open serves the saved surprise result instead of a live sweep (api-gateway).
  Item 1 of the post-deploy startup-log review. Real-trade's first `/surprise/scan?cached=true` at 08:58 IST found a 17.6 h old result; the 08:30-09:15 window was excluded from the group 138 shortcut, so a full ~1,000-quote live scan started, the caller waited 20 s for the same old rows, and the scan kept `/quote` busy (the `lane budget shed` burst). Now, more than `SURPRISE_PREOPEN_STALE_LEAD_SEC` (default 300 s, 0 = off; `SURPRISE_PREOPEN_STALE_SERVE=0` turns it off) before 09:15, `scan(cached=True)` returns the saved result at once with `preopen_stale_cache: true`; the last 5 minutes, the session and per-symbol requests are unchanged. Also corrects my group 222 note: `/angelone/movers` did not sweep twice (the second gateway line is a 6 h cache hit), so no movers change. Suite not run in the sandbox (no pytest/sqlalchemy); new tests passed under a stand-in runner. Doc: docs/GROUP223_PREOPEN_STALE_SURPRISE_SERVE.md. Tests: api-gateway tests/test_surprise_scanner.py (+22 in TestPreopenStaleServe, one pre-open test narrowed).
- 2026-10-07 (group 222)  boot quote burst skipped for early pre-open boots, and /quote logs why AngelOne-first missed (api-gateway + market-data-service).
  Item 2 of the 2026-10-07 startup-log review. The group 120 boot-sweep skip only covered closed/holiday, so an 08:37 IST (pre-open) boot still swept ~1,000 symbols through /quote; a pre-open boot more than `SURPRISE_BOOT_WARM_PREOPEN_LEAD_SEC` (default 300 s, 0 = old behaviour) before 09:15 now restores the saved result and skips. `AngelOneSession.get_quote` now records the specific miss cause (cooldown, global 403 cooldown, lane shed, no token in N s, rate-limited answer, no quote for token) and the "AngelOne-first did not price" line prints it. Movers' 2,584-quote sweep is untouched (likely cause, not proven). Real suites not run in the sandbox (no pytest/fastapi/httpx); the new tests passed under a stand-in runner. Doc: docs/GROUP222_BOOT_QUOTE_BURST_AND_MISS_REASON.md. Tests: api-gateway tests/test_group222_boot_warm_preopen.py (new, 20), market-data tests/test_group222_quote_miss_reason.py (new, 14), tests/test_main_ws_loops_startup.py (fixture pins the clock).
- 2026-10-07 (group 221)  scalp market gate sees the previous close (api-gateway + position-stocks-service).
  Review item 8 (Nifty-gate half). `/market/indices` `change_pct` is really "vs today's open" because `history(period="1d")` is one row and the previous-close branch never runs; a market that gapped down 1.2% and stayed flat read 0.0% and passed the gate. The gateway now also returns `nifty_vs_prev_close` / `sensex_vs_prev_close` (real previous close via a 5d read); the scalp gate additionally blocks at or below `MARKET_GATE_MIN_NIFTY_PREV_CLOSE_PCT` (default -0.75, my assumption), ignores stale/fallback bodies and fails open; `MARKET_GATE_PREV_CLOSE_ENABLED=0` restores the old gate. `change_pct`, `market_score` and real-trade-service are deliberately unchanged (decision left to you). Gateway endpoint tests not run in the sandbox (no fastapi). Doc: docs/GROUP221_NIFTY_PREV_CLOSE_GATE.md. Tests: position-stocks tests/test_group221_prev_close_gate.py (new, 34), api-gateway tests/test_group221_indices_prev_close.py (new).
- 2026-10-07 (group 220)  opening entry guard (real-trade-service).
  Review item 6. New `entry_engine/opening_guard.py`: while the market is open and before 09:30 IST, `evaluate_mode()` returns before its candidate loop, so candidates stay queued (not consumed, rejected or logged) and are evaluated normally once the guard lifts. Covers the regular auto-pilot cycle too (two of the five September open entries fired at 09:19, before ENTER_AT_OPEN's 09:20). The ENTER_AT_OPEN once-a-day trigger moves to the guard time so its run is not wasted. ON by default for REAL and DEMO; `OPENING_ENTRY_GUARD_ENABLED=false` turns it off, `OPENING_ENTRY_NOT_BEFORE_IST`, `OPENING_ENTRY_GUARD_MODES`. Evidence is weak (5 trades; 4 of them base VOLUME_SHOCK, which Gate 1b already holds back) so it is a precaution, not a proven fix. Exits, manual_engine orders and the scalp service are untouched; open-gap check not done. Doc: docs/GROUP220_OPENING_ENTRY_GUARD.md. Tests: tests/test_group220_opening_guard.py (new, 35), tests/test_group220_enter_at_open_schedule.py (new, 6); tests/conftest.py switches the guard off for older tests.
- 2026-10-07 (group 219)  scalp ranking v2 (position-stocks-service).
  Review item 4. `screening/engine.py` score is now pct_eff x liquidity x volume_pace x range x vwap x consistency x window: the move's contribution is capped at 3x the window threshold (so the biggest mover no longer wins on size), the volume weight that saturated at 150k shares is replaced by a liquidity factor times a within-day volume-pace weight (0.5-2.0, neutral when unknown) built from cumulative-volume snapshots recorded in `on_tick_hook`. `Candidate.pct_change` and all gates are unchanged; `Candidate.rvol` added. ON by default, unvalidated on outcomes; `SCAN_RANKING_V2_ENABLED=0` restores the old formula. Not true day-over-day relative volume (no history). Doc: docs/GROUP219_SCALP_RANKING_V2.md. Tests: tests/test_group219_ranking_v2.py (new, 34); tests/test_screening_engine.py fixture pins the legacy formula.
- 2026-10-07 (group 218)  scalp entry cost gate (position-stocks-service).
  Review item 5. New `orders/cost_gate.py`, called from `attempt_entry` once the quantity is known: skips an entry when expected edge at the target is under 3x its round-trip cost (INTRADAY levies + a 0.10% slippage allowance), with a `COST_GATE:` skip reason and clean release of capital and symbol lock. The forced first live order and manual entries are exempt. Same statutory env names as real-trade-service (`BROKERAGE_PER_ORDER` etc.). At the default Rs 0 brokerage it passes almost everything (cost about 0.14% vs 1.4-3.5% targets); it matters once brokerage is set. Also corrects the review's "0.1% round trip" (levies alone are about 0.04%). `SCALP_COST_GATE_ENABLED=0` turns it off. Doc: docs/GROUP218_SCALP_ENTRY_COST_GATE.md. Tests: tests/test_group218_cost_gate.py (new, 20), tests/test_entry.py (+5).
- 2026-10-07 (group 217)  Position Stocks trailing stop (position-stocks-service).
  Review item 2. New `orders/trailing.py`: once a trade's peak is +1% over entry, the Super Order STOP_LOSS_LEG follows the peak (distance = max(0.4%, 0.6 x its own stop %)), only ever upward, never below entry + 2 ticks, always under the live price, target leg untouched. Sets `stop_moved_to_breakeven` so breakeven cannot undo it; `reconcile._apply_entry_correction` no longer lowers a ratcheted stop. ON by default; `TRAILING_STOP_ENABLED=0` turns it off, six `TRAIL_*` tuning knobs. Doc: docs/GROUP217_TRAILING_STOP.md. Tests: tests/test_trailing.py (new, 32), tests/test_reconcile.py (+2).
- 2026-10-07 (group 216)  scalp entry slippage tolerance halved (position-stocks-service).
  Review item 3. `ENTRY_MAX_SLIPPAGE_PCT` default 0.5 -> 0.25 (the stop is only 0.8-2.0%), `ENTRY_FILL_SLIPPAGE_ALERT_PCT` 1.0 -> 0.5. Config defaults only; env restores the old values. Legs were already re-armed from the real fill by reconcile (2026-09-18), so nothing changed there. Doc: docs/GROUP216_ENTRY_SLIPPAGE_TOLERANCE_TIGHTENED.md. Tests: tests/test_entry.py (+2), tests/test_scalp_review_20261005.py (pins).
- 2026-10-06 (group 215)  entry cash is claimed best-conviction-first instead of first-come (real-trade-service).
  Audit item A7 of group 155 ("capital check order"). `evaluate_mode` reserved cash in `received_at` order before Gate 6 ranked anything, so a weak early candidate could starve a stronger later one. New `_order_candidates_for_capital` walks the batch by conviction (stable, ties keep received order); gates, cash maths and Gate 6 unchanged. `ENTRY_CAPITAL_ORDER_BY_CONVICTION=0` restores the old order. Doc: docs/GROUP215_CAPITAL_CLAIMED_BEST_CONVICTION_FIRST.md. Tests: real-trade-service/tests/test_group214b_capital_order.py (13).
- 2026-10-06 (group 214)  volume-shock quality gate no longer passes a candidate it could not score at all (real-trade-service).
  Audit item A6 of group 155 ("quality-gate fail-open"). `_fetch_fund_tech_score` now reports `fund_fetched`/`tech_fetched`; a symbol in the scored batch whose two lookups both failed (timeouts, non-200, per-symbol exception, or the whole scoring pass failing) is skipped this cycle and retried next, instead of "floor check skipped" = pass. Unchanged: service answered with no score stays lenient, one working lookup still applies its floor, candidates beyond `VOLUME_SHOCK_QUALITY_GATE_MAX_SYMBOLS` stay ungated. `VOLUME_SHOCK_QUALITY_FAIL_CLOSED=0` restores the old behaviour. Doc: docs/GROUP214_QUALITY_GATE_UNSCORED_SKIPPED.md. Tests: real-trade-service/tests/test_group214_quality_gate_unscored.py (new), test_candidates_orchestration.py (2 changed).
- 2026-10-06 (group 210)  stale symbol locks are freed while the service runs, not only at restart (position-stocks-service).
  Item 14 of the open list, first half. New `shared_symbol_lock.sweep_stale` (young claims and dead-exit-SELL locks kept, throttled, never raises) called from the fast reconcile loop; `cleanup_stale` at startup unchanged (`SYMBOL_LOCK_SWEEP_INTERVAL_S` 60, `SYMBOL_LOCK_SWEEP_MIN_AGE_S` 600, 0 = off). COHANCE half not changed: the priority lane comes from OPEN rows in `trade_positions`, needs that row. Doc: docs/GROUP210_SYMBOL_LOCK_SWEEP.md. Tests: position-stocks-service/tests/test_group210_symbol_lock_sweep.py (10).
- 2026-10-06 (group 209)  A margin rejection pauses all new entries for 5 min; a failed order placement rests that symbol 5 min (position-stocks-service).
  Item 15 of the open list, second half. New `orders/entry_pause.py`; `attempt_entry` and `reconcile._learn_from_entry_rejection` start the pauses (`ENTRY_MARGIN_PAUSE_MINUTES`, `ENTRY_ORDER_FAILED_COOLDOWN_MINUTES`, 0 = off); margin-rejected rows no longer count against the symbol's group-192 cooldown/day cap. Doc: docs/GROUP209_ENTRY_MARGIN_PAUSE.md. Tests: position-stocks-service/tests/test_group209_entry_pause.py (14) plus an autouse reset in conftest.py.
- 2026-10-06 (group 208)  Live Dhan panel: only open positions, real LTP, no cost-as-P&L; capital card no longer shows a false ₹0 (real-trade-service + frontend).
  Item 13 of the open list. New `portfolio/broker_view.py`; `/dhan/positions` adds `rows`/`open_count` with priority-lane LTPs; `RealAutoTrade.tsx` and `CapitalSplitCard.tsx` updated. Ledger drift not changed (needs your numbers). Doc: docs/GROUP208_BROKER_PANEL.md. Tests: real-trade-service/tests/test_group208_broker_view.py, test_main_routes_trading.py (12 new/changed).
- 2026-10-06 (group 207)  Trade History totals count only settled trades; breakeven is not a loss (position-stocks-service + frontend).
  Item 12 of the open list. New `orders/trade_stats.py`; `/trades/history` summary gains `breakeven`, `pending_reconcile`, `error_trades` and excludes placeholder/ERROR rows from win rate and P&L; `/trades/breakdown` skips placeholders; dashboard card shows BE and the not-counted line. Doc: docs/GROUP207_TRADE_SUMMARY_COUNTS.md. Tests: tests/test_group207_trade_stats.py, test_main.py (16 new).
- 2026-10-06 (group 206)  a rejected duplicate SELL no longer turns a filled trade into ERROR (position-stocks-service).
  Item 11 of the open list (AVALON). `_fire_flat_sell` skips the SELL when the bracket exit already filled and adopts an already-placed order on retry; `_reconcile_eod_pending` looks for the real exit before writing ERROR; `POST /reconcile/repair-dead-sell-errors` (dry run by default) repairs today's damaged rows. Doc: docs/GROUP206_DUPLICATE_SELL_ERROR.md. Tests: position-stocks-service/tests/test_reconcile.py, test_eod_squareoff.py (19 new/changed).
- 2026-10-06 (group 205)  flat-SELL exits keep their pending marker, so the real fill is booked instead of a permanent ₹0 (position-stocks-service).
  Item 10 of the open list. `reconcile.py`: the super-order fallback no longer clears `*_PENDING_RECONCILE` when the flat SELL is not resolvable yet; new `rearm_cleared_placeholder_exits` + `POST /reconcile/rearm-placeholder-exits` (dry run by default) repairs rows already damaged. Doc: docs/GROUP205_PENDING_MARKER_KEPT.md. Tests: position-stocks-service/tests/test_reconcile.py (8 new/changed).
- 2026-10-06 (group 204)  one Yahoo live socket, one subscribe, no lost universe (market-data-service).
  Item 5 of the open list. `yahoo_ws_feed.py`: thread start is atomic under a lock; a clean `listen()` return now closes the old socket, logs and waits 5 s instead of silently re-subscribing; a `_DESIRED` set means reconnects and idle periods keep the refreshed universe (it used to fall back to the boot list); the client is published only after the first subscribe; subscribe lines say why (`connection #N, <reason>`). Doc: docs/GROUP204_YAHOO_WS_SINGLE_SOCKET.md. Tests: market-data-service/tests/test_group204_yahoo_ws_single_socket.py (14, run under a stand-in runner, not real pytest).
- 2026-10-06 (group 203)  cold-start /scan/universe waits 5 s, not 12 s, for movers (api-gateway).
  Item 5 of the open list. After the restart three `/scan/universe` callers each waited the full 12 s movers deadline while the startup warm pass was still running, then got no movers anyway. Until a movers pass has produced a list in this process (`_MOVERS_EVER_READY`), `_movers_with_deadline` waits `SCAN_UNIVERSE_COLD_MOVERS_DEADLINE_S` (5; 0 = off); afterwards the old 12 s. Trade-off: movers that would have arrived at 6-12 s right after boot are missed by that caller (none arrived in the one log we have). New `tests/test_group203_cold_movers_deadline.py` (18; 17 run against extracted code under a stand-in runner, existing suites not run). Doc: `docs/GROUP203_COLD_START_MOVERS_DEADLINE.md`. Rebuild api-gateway.
- 2026-10-06 (group 202)  NSE movers log line shows rows returned per board (api-gateway).
  Item 4 of the open list. The NSE degradation is the datacenter-IP block (groups 178/195: pause + stale cache already in place, nothing left in code); what was missing is telling "+0 symbols" apart: empty board vs quiet board. `main.py::_get_momentum_movers` now logs `NSE movers <endpoint>: +N symbols (rows=R, under 2% move=Q)`; behaviour unchanged. 6 new tests `test_group202_*` in `tests/test_main_universe.py`, suite not run (no pytest/fastapi in sandbox), edited loop run with stubs. Doc: `docs/GROUP202_NSE_MOVERS_ROW_COUNTS.md`. Rebuild api-gateway.
- 2026-10-06 (group 201)  thin news: the bare-ticker company name expires, thin Google News searches get a second query (analysis-intelligence-service).
  Item 3 of the open list. `event/main.py::_get_company_name` cached a bare-ticker fallback (yfinance rate-limited / no name) for the whole process life, so Google News kept being searched by ticker after yfinance recovered; it is now remembered for `EVENT_COMPANY_NAME_FALLBACK_TTL_S` (600 s, 0 = old). `_fetch_google_news` makes a second, differently worded search when the first returns fewer than `EVENT_GN_THIN_BELOW` (3, 0 = off) items and merges the two (not when the first search itself failed). Not live-tested: no network here, effect on item counts unverified; new INFO line `Google News widened for ...`. Tests: `tests/test_group201_news_coverage.py` (31, run under a stand-in runner), QUIRK test rewritten, conftest autouse `EVENT_GN_THIN_BELOW=0`; `test_event_main.py` not run. Doc: `docs/GROUP201_NEWS_COVERAGE_COMPANY_NAME_AND_WIDER_SEARCH.md`. Rebuild analysis-intelligence-service.
- 2026-10-06 (group 200)  AngelOne-first quote rows are really cached now, and misses say why (market-data-service).
  The post-199 report shows SGRL/KENNAMET/ROSSTECH at 0.2 s via `angelone_rest`, but quote #2 re-fetched from AngelOne (new `fetched_at`, 0.5-2.2 s): group 199 stored rows with ttl=12, inside `_should_soft_refresh`'s 45 s window, so every repeat call refetched. Rows now use ttl 12+45=57 s. New `_ao_first_miss` logs one INFO line per symbol per 5 min with the reason (cooldown / no token / empty answer / no ltp / error) because STEAMHOUSE still took 20.5 s from Yahoo and the report cannot say why. Tests: 32 in `tests/test_group199_quote_angelone_first.py` (5 new, 2 verified to fail on the old ttl); market-data 940 passed. Not live-tested. Doc: `docs/GROUP200_ANGELONE_FIRST_CACHE_AND_MISS_LOG.md`.
- 2026-10-06 (group 199)  GET /quote asks AngelOne REST first for symbols outside the live feed (market-data-service).
  Item 1. The group 198 report shows STEAMHOUSE / SGRL / KENNAMET / ELEVATE / ROSSTECH all have `-EQ` rows (SME theory wrong) and all price, but the four outside the 489-symbol feed took 5-20 s on `/quote` (Yahoo first), long enough for real-trade-service's ReadTimeout; `/quotes/bulk` (AngelOne REST) recovered them. New `_angelone_rest_quote_first` in `_get_quote_inner` before Yahoo: one token, 6 s cap, `angelone_client.get_quote(max_wait=)` fail-closed on a busy bucket; any miss falls through to the unchanged Yahoo path; `QUOTE_ANGELONE_FIRST=0` turns it off. No change to the pause logic or the universe. Tests: `tests/test_group199_quote_angelone_first.py` (27); market-data 935 passed. Not live-tested. Doc: `docs/GROUP199_QUOTE_ANGELONE_FIRST.md`.
- 2026-10-06 (group 198)  diagnostic for symbols that never price (scripts only, no service change).
  Item 3 of the open list. The pause (group 160/183b) and the "no price" cache (group 161) already exist; what the repo cannot say is why STEAMHOUSE / SGRL / KENNAMET have no price (SME series, wrong ticker, or failing sources; the bhavcopy keeps only EQ/BE/BZ). New `scripts/diagnose_never_priced.sh` + `.py` print each name's AngelOne series rows, timed `/quote` and `/last-close` answers, the log lines in three services, and whether it is in the scan universe / movers. Details in `docs/GROUP198_NEVER_PRICED_DIAGNOSTIC.md`. Nothing to rebuild.
- 2026-10-06 (group 197)  force_refresh=true now really rebuilds the scan universe (api-gateway).
  Closes the "not changed" gap of group 196. `run_scan`, `start_scan` and the scan stream dropped the live universe key on `force_refresh` but then called `_build_scan_universe()`, which re-served the stored stale copy. New `_build_scan_universe_forced()` runs the real rebuild (falls back to the plain call if one is already running or it returns nothing). New tests (4); details in `docs/GROUP197_FORCE_REFRESH_REAL_UNIVERSE_REBUILD.md`. Rebuild api-gateway.
- 2026-10-06 (group 196)  a stale-served scan universe now gets a real background rebuild (api-gateway).
  Item 2 of the open list ("slow first scan after a restart"). `_build_scan_universe()` served the durable stale copy when the live key was cold and promised a background rebuild, but every rebuild path called the same function, which found the stale copy again, so once a stale copy existed the universe was re-served and never rebuilt. New `_build_scan_universe_fresh()` (real rebuild, single-flight) and `_schedule_scan_universe_refresh()` (daemon thread, started on every stale serve; `SCAN_UNIVERSE_STALE_REFRESH=0` turns it off); the cached=true route and the startup warm use the real rebuild; a rebuild under 50 symbols never overwrites the stored universe. New tests (24); details in `docs/GROUP196_STALE_UNIVERSE_BACKGROUND_REBUILD.md`. Rebuild api-gateway.
- 2026-10-06 (group 195)  tolerant RSS parsing in the after-hours scan, Yahoo news auto-pause (real-trade-service, analysis-intelligence-service).
  Item 1 of the open list. BusinessStandard's RSS (HTTP 200, real XML) was dropped as "non-XML" for one bad character; the scan now repairs it (control characters, bare `&`) or reads items by regex, warning only when nothing can be read. The Yahoo news source returned 0 items for 40+ symbols in a row; it is now skipped for 30 min after 30 empty answers (`EVENT_YF_NEWS_EMPTY_PAUSE_AFTER`, `EVENT_YF_NEWS_PAUSE_SECONDS`), with a probe afterwards. The NSE "weak cookie" line is informational (NSE data still arrived) and was left alone. New tests (12 + 7); details in `docs/GROUP195_TOLERANT_RSS_AND_YAHOO_NEWS_PAUSE.md`. Rebuild real-trade-service and analysis-intelligence-service.
- 2026-10-06 (group 194)  /quotes/bulk stops serving stale cached rows (market-data-service).
  Item 1 of the open list. real-trade rejected 183 of 725 bulk rows as older than its 20 s limit (the cache kept them with their original `fetched_at`) and re-priced each through `GET /quote`; that burst starved market-data and caused the ReadTimeouts. Rows older than `BULK_CACHE_MAX_AGE_SEC` (15, 0 = off) now go through the live feed / AngelOne REST batch; the old row is returned only if every source fails (`stale_served`). Also documents that 172.18.0.7 is api-gateway. New tests (9); sandbox: market-data 908 passed (899 before); details in `docs/GROUP194_BULK_QUOTES_STALE_CACHE_AND_CALLER_MAP.md`. Rebuild market-data-service.
- 2026-10-06 (group 193)  the 50% share-cap total no longer shrinks (real-trade-service, position-stocks-service).
  Priority 1 of the open-issue list (almost every real-trade candidate WAIT on `capital_share_cap`). BUYs Dhan is still filling were counted nowhere although Dhan blocks their cash, and position-stocks published its exposure only at the end of a successful gated sync, so the total undercounted. real-trade now counts in-flight BUYs (`SHARE_CAP_IN_FLIGHT_MAX_AGE_MINUTES` 120, 0 = off) and the reject message shows every component and the peer figure's age; position-stocks publishes from its own DB first in every sync and every ~6 s from the fast loop; publish sets a heartbeat `updated_at`. New tests (24 + 7); sandbox: position-stocks 2682 passed, real-trade 3359 passed + the 5 that also fail on the uploaded zip; details in `docs/GROUP193_SHARE_CAP_TOTAL_NO_LONGER_SHRINKS.md`. Rebuild real-trade-service and position-stocks-service.
- 2026-10-06 (group 192)  rejected entry orders are learned from, not retried (position-stocks-service, frontend).
  Priority 1 of the 2026-10-06 open-issue list: HEGAM was bought 13 times in four minutes, each rejected by Dhan RMS ("not allowed to be traded in Intraday"). Dhan accepts the Super Order and rejects it afterwards, so only `orders/reconcile.py` saw it, as a generic "Entry leg REJECTED" ERROR row that recorded no reason and no restriction. Reconcile now stores Dhan's reason and records an intraday-restricted / circuit-limit rejection in the restricted-symbol list; `orders/entry.py::_rejected_entry_reject` skips a symbol after a dead entry (`ENTRY_REJECT_COOLDOWN_MINUTES` 30, `ENTRY_REJECT_MAX_PER_SYMBOL_DAY` 2, 0 = off); `/trades/history` summary gains `rejected_entries`; the dashboard moves rejected entries out of "Closed Today". New `tests/test_group192_entry_reject_learning.py` (22 cases); sandbox: position-stocks 2675 passed (2653 on the uploaded zip), tsc clean; details in `docs/GROUP192_ENTRY_REJECT_LEARNING.md`. Rebuild position-stocks-service and the frontend.
- 2026-10-06 (group 191)  "CANDIDATE REJECTED" log lines no longer cut at 150 characters (real-trade-service).
  Item 17 of the 2026-10-06 boot-log list (REDINGTON ended at "Wait for a breakout or", HFCL before its closing brace). Not the logger: `candidate_engine/candidates.py` printed `reject[:150]` on both the standard and the volume-shock REJECTED lines, while the reasons embed the whole returns dict plus advice text. New `_reject_log_text` (`CANDIDATE_REJECT_LOG_MAX`, default 500, 0 = never cut; a cut line ends in " ..."). The stored reason was never cut. New `tests/test_group191_reject_log_not_cut.py` (6 cases); sandbox: real-trade 3335 passed + the 5 that also fail on the uploaded zip; details in `docs/GROUP191_REJECT_LOG_NOT_CUT.md`. Rebuild real-trade-service.
- 2026-10-06 (group 190)  silence the dhanhq pandas DtypeWarning (real-trade-service, position-stocks-service).
  Item 18 of the 2026-10-06 boot-log list. `execution/dhan_client.py::_fetch_security_list_quiet` wraps the SDK's `fetch_security_list(mode="compact")` call and ignores only the `Columns (...) have mixed types` warning (old and new pandas wording) for that call; other warnings, the filter list and SDK exceptions are untouched. New `tests/test_group190_security_list_quiet.py` in both services (5 cases each); sandbox: dhan_client test files pass, real-trade full suite 3329 passed + 5 failing that also fail on the uploaded zip; details in `docs/GROUP190_SECURITY_LIST_DTYPE_WARNING.md`. Rebuild real-trade-service and position-stocks-service.
- 2026-10-06 (group 189)  one momentum-movers pass at a time (api-gateway).
  Item 12 of the 2026-10-06 boot-log list: the NSE boards + AngelOne sweep + `momentum_movers step1` sequence ran twice at once because the 90 s result cache is only filled when a pass finishes. `main.py::_get_momentum_movers` is now a single-flight wrapper around `_compute_momentum_movers` (unchanged body): later callers wait for the running pass (`MOMENTUM_MOVERS_JOIN_WAIT_S`, 45 s), a failed or slow leader makes them compute for themselves as before, re-entrant calls never wait on themselves; `MOMENTUM_MOVERS_SINGLE_FLIGHT=0` restores one pass per caller. Per process. New `tests/test_group189_momentum_movers_single_flight.py` (10 cases); sandbox: gateway 8296 passed + the 6 order-dependent `test_searched_list_selfclean.py` failures that also fail on the uploaded zip; details in `docs/GROUP189_MOMENTUM_MOVERS_SINGLE_FLIGHT.md`. Rebuild api-gateway.
- 2026-10-06 (group 188b)  ETFs and funds out of the feed universe and the AngelOne sweeps (api-gateway, market-data-service, position-stocks-service).
  Item 9 of the 2026-10-06 boot-log list (BANKBETA, MONQ50, LIQUIDPLUS, SILVERADD, GSEC10YEAR, AONESILVER, GROWWMETAL ...). New `instrument_filter.is_etf_or_fund` (name test: ETF/BEES suffixes, LIQUID, GSEC, SILVERADD/GOLDCASE, a short explicit list; `ETF_FUND_EXTRA_SYMBOLS`, `ETF_FUND_FILTER=0`) applied to market-data's feed universe and `/angelone/movers` token list and to position-stocks' WebSocket subscription; the gateway regex got the same patterns. Name-based only, so an unrecognised ETF still gets through. 3 new test files; sandbox: market-data 899 passed, position-stocks 2647 passed + 1 failing that also fails on the uploaded zip, api-gateway 8286 passed + 6 order-dependent failures in `test_searched_list_selfclean.py` that also fail on the uploaded zip; details in `docs/GROUP188B_ETF_FUND_UNIVERSE.md`. Rebuild all three.
- 2026-10-06 (group 188)  stop asking Yahoo for NAME.BO when NAME.NS has nothing (market-data-service).
  Item 10 of the 2026-10-06 boot-log list (~15 "$CHEMICAL.BO / AJOONI.BO ... possibly delisted; no price data found"). `main.py::_yahoo_tickers_for` no longer adds `.BO` for a symbol that is in the NSE scrip master (new `angelone_scrip_master.is_listed`, never waits; `YAHOO_SKIP_BO_FOR_NSE=0` restores it), and a `.BO` ticker with empty history is remembered for `YAHOO_BO_MISS_TTL_S` (21600 s, 0 = off) in both Yahoo history paths. New `tests/test_group188_yahoo_bo_misses.py` (23 cases); `tests/conftest.py` clears the memory between tests; full market-data-service suite passes in the sandbox; details in `docs/GROUP188_YAHOO_BO_MISSES.md`. Rebuild market-data-service.
- 2026-10-06 (group 187)  market-cap fetch says why it failed and keeps the last good value (real-trade-service).
  Items 4-5 of the 2026-10-06 boot-log list. `candidate_engine/candidates.py::_fetch_market_cap_cr`: the INFO line names the cause (ReadTimeout / HTTP code / no market_cap) instead of `()`, and the last good market cap per symbol is reused for `CANDIDATE_MCAP_STALE_TTL_S` (24 h) when a fetch fails, so a timeout no longer drops the market-cap floor; `CANDIDATE_MCAP_TIMEOUT_S`. Item 4 (fundamentals ReadTimeout) needs no code change: analysis-intelligence already uses 60 s + md_guard, the cause is market-data load. New `tests/test_group187_market_cap_failure_reason.py`; details in `docs/GROUP187_MARKET_CAP_FAILURE_REASON.md`. Rebuild real-trade-service.
- 2026-10-06 (group 186)  candidate timeframe sanity, "incomplete history" reason, bhavcopy miss memory (real-trade-service, market-data-service).
  Items 1-3 of the 2026-10-06 boot-log list. `candidate_engine/candidates.py`: a 1d/1w/1m return far above the next longer horizon (HFCL 1w +211% vs 1m +4.7%) is dropped and logged instead of scoring as bullish (`CANDIDATE_TF_SANITY`, `CANDIDATE_TF_CAP_*_PCT`); when missing horizons could still reach the threshold the reject reason is "Incomplete history, cannot judge" (`data_incomplete`) instead of "weak momentum" (TCS with 5 horizons missing). `market-data-service/bhavcopy.py`: a symbol absent from all recent bhavcopy days is remembered for `BHAVCOPY_EOD_MISS_TTL_S` (FOCUS/TECH); who asks for those words is still unknown. New `tests/test_group186_tf_sanity_incomplete_history.py`, `TestEodMissMemory`; details in `docs/GROUP186_TF_SANITY_INCOMPLETE_HISTORY.md`. Rebuild real-trade-service and market-data-service.
- 2026-10-06 (group 185)  boot warning when the NSE holiday list has run out (api-gateway).
  The "2027 holiday dates in December" item. The dates are NOT entered: NSE publishes next year's circular in December and the third-party 2027 lists disagree. Instead `nse_holidays.py::holiday_coverage_warning()` (this year with no dates, or next year from 15 Nov) is logged as a WARNING at gateway startup, naming `scripts/check_holiday_lists_sync.py` (seven copies, all agree today). From 15 Nov 2026 every boot will say so until 2027 is added. 6 new tests in `tests/test_group185_holiday_coverage_warning.py` pass directly; full suite not run (no pytest in sandbox); details in `docs/GROUP185_HOLIDAY_LIST_COVERAGE_WARNING.md`. Rebuild api-gateway.
- 2026-10-06 (group 184)  data polls pause while the browser tab is hidden (frontend).
  The last "per-tab polling" item. A background browser tab kept running ~10 polls per round. New `src/visibleInterval.ts::setVisibleInterval` (skips ticks while the page is hidden, one refresh when it is shown again, cadence unchanged while visible) now drives the data polls in `PositionStocksTab` (7), `RealAutoTrade` (4), `App.tsx` overnight panel, `RateLimitDashboard`, `ServiceManager`, `MarketSentimentHeader`, `DecisionCard`. No visible tab is slower; clocks, job-progress polls and websocket code are unchanged. `frontend/scripts/check_visible_interval.ts` (7 checks) passes; the edited files parse; full `tsc` NOT run (no React types offline) and not browser-tested; details in `docs/GROUP184_POLLING_PAUSES_WHEN_BROWSER_TAB_HIDDEN.md`. Rebuild the frontend image.
- 2026-10-06 (group 183b)  dead-symbol and no-history pauses survive a restart (real-trade-service).
  Item 7 of the open list, both pauses. New `resilience/pause_state.py` saves the group 160 dead-symbol pause (`market_feed/feed.py`) and the group 172 no-daily-history pause (`candidate_engine/candidates.py`) in `trade_resilience_cache` as wall-clock deadlines (debounced, own DB session in a daemon thread, nothing written before the startup load); `main.py` restores both at boot, keeping the miss count so the backoff carries on. `PAUSE_STATE_PERSIST=0` = per-process as before. 20 new tests in `tests/test_group183b_pause_state_persist.py` all pass via a stand-in runner; the existing group 160/172 files and the full suite were NOT run (no httpx/sqlalchemy/pytest in sandbox); details in `docs/GROUP183B_PAUSES_SURVIVE_RESTART.md`. Rebuild real-trade-service.
- 2026-10-06 (group 183)  ipoalerts remembers a status the plan rejects (api-gateway).
  From the boot log: `ipoalerts status=listed -> HTTP 400 ... not supported for free plan users`. `ipo_scanner.py::fetch_ipoalerts_calendar` asked for `listed` on every cache miss and each refused call still cost one of ~25 daily requests. A 400 saying the plan does not support the parameter now marks that status in kv for `IPOALERTS_UNSUPPORTED_TTL_S` (7 days, survives restart, 0 = old behaviour); later fetches skip it and the quota check counts only statuses still asked. Other failures are not marked. 8 new tests in `tests/test_ipo_scanner.py` (with the 14 existing ipoalerts cases run via a stand-in runner, all pass; full suite not run, no pytest in sandbox); details in `docs/GROUP183_IPOALERTS_REMEMBER_REJECTED_STATUS.md`. Rebuild api-gateway.
- 2026-10-06 (group 182)  Reconnect/Connect box on the unreachable-backend screens (frontend).
  Last carried-over item. `src/api.ts`: new `normalizeGatewayUrl()` used by `setApiUrl` (a bare hostname was stored as a relative path, so requests hit the page's own origin; now gets `https://`, or `http://` for localhost/private hosts, trailing slashes trimmed). `components/SystemCheck.tsx`: Reconnect now restarts the connecting timer (same phase meant no visible effect) and each Connect/Reconnect starts a numbered check chain so a hung older check can no longer overwrite the new attempt. `tsc --noEmit` clean; new `frontend/scripts/check_gateway_url.ts` (18 cases, run with `npx tsx`) passes; the screen flow is NOT browser-tested (no frontend test runner); details in `docs/GROUP182_RECONNECT_GATEWAY_URL_AND_STALE_CHECKS.md`. Rebuild the frontend image.
- 2026-10-06 (group 181)  scalp pool credits its share of real-trade-service's open positions (position-stocks-service).
  Carried-over item "scalp pool shrinking when real-trade buys". `capital/ledger.py::sync_from_broker`: pool baseline was 50% of Dhan FREE cash + own committed, so a real-trade buy (free cash falls) shrank it by half of the spend with the account value unchanged (100k account, real buys 40k: pool 30k instead of 50k). Now adds the pool's share of real-trade-service's published open-position value (`shared_exposure.get_other_service_exposure`, was unused here), capped at free cash + own committed, fail-open; one INFO line; `SCALP_POOL_CREDIT_PEER_EXPOSURE=0` restores the old figure. `available_capital` rule unchanged (reset only when no scalp position is open). 13 new tests in `tests/test_group181_ledger_peer_exposure_credit.py`; full position-stocks-service suite 2606 passed in the sandbox; details in `docs/GROUP181_SCALP_POOL_CREDITS_PEER_EXPOSURE.md`. Rebuild position-stocks-service.
- 2026-10-06 (group 180)  same-day CNC import capped to Dhan's net quantity (real-trade-service).
  Carried-over item "POWERGRID imported as a same-day CNC position": no log evidence and the import path is deliberate, so no change to WHETHER it imports; details of what you saw are still needed. Found and fixed a real flaw next to it: `portfolio/portfolio.py::import_broker_holdings` used the buy-side quantity for same-day CNC rows, so a part-sold row was imported too large and a sold-out row (netQty 0, positionType CLOSED) became a ghost position. Now net <= 0 is skipped and net < buy imports the net quantity; no netQty / unparseable / equal is unchanged; `IMPORT_CNC_CAP_TO_NET_QTY=0` restores the old behaviour. 9 new cases in `tests/test_portfolio.py`; sandbox: portfolio + re-import-skip tests 101 passed; details in `docs/GROUP180_SAME_DAY_CNC_IMPORT_NET_QTY.md`. Rebuild real-trade-service.
- 2026-10-06 (group 179)  Dhan security list loaded in the background (real-trade-service).
  Carried-over item "Dhan scrip-master load on the first order". `execution/dhan_client.py`: new `keepwarm_security_cache()` task started from `main.startup()` loads the list ~10 s after boot and refreshes it after 18 h (TTL 24 h), retrying in 5 min after a failure and re-checking every 30 min while no Dhan credentials are stored; own DB session in a worker thread, never raises. `get_security_id()` unchanged (still loads synchronously if the cache is empty). `DHAN_SECURITY_WARM_ENABLED=0` turns it off. 15 new tests in `tests/test_group179_security_cache_warmup.py`; full single-process run of the suite shows the same 4 failures + 1 error in the group 171/172 files as the group 173 code does in this sandbox (each passes alone); details in `docs/GROUP179_DHAN_SECURITY_LIST_BACKGROUND_WARMUP.md`. Rebuild real-trade-service.
- 2026-10-06 (group 178)  NSE boards pause while blocked, AngelOne movers error cached and logged (api-gateway, market-data-service).
  Item 6 of the open list. The 403s themselves are an NSE IP block / AngelOne login refusal and are NOT fixed in code. api-gateway `main.py`: after a 401/403/429 that survives the session-refresh retry, all NSE api fetches pause for `NSE_API_BLOCK_SECONDS` (600 s, 0 = off), stale cache still served, one warning at pause start; `AngelOne movers unavailable (status=...)` warning (once per 10 min) for status=error/not_configured answers that were silently ignored. market-data-service `main.py`: a failed `/angelone/movers` sweep is cached for `ANGELONE_MOVERS_ERROR_TTL_S` (120 s, 0 = off) so callers stop re-logging in, and the error text includes the exception type. Tests: `TestNseApiBlockPause` (test_main_core.py), 2 in test_main_universe.py, `test_group178_angelone_movers_error_cache.py`; sandbox full suites: api-gateway 8247 passed, market-data-service 826 passed; details in `docs/GROUP178_NSE_BLOCK_PAUSE_AND_ANGELONE_MOVERS_ERROR.md`. Rebuild api-gateway and market-data-service.
- 2026-10-06 (group 177)  watchlist Tier 1: says why it fell through, fetches both routes together, keeps Hot Picks when only the IPO list fails (real-trade-service).
  Item 4 of the open list. `watchlist_engine/sources.py`: `/stockky-hot` and `/surprise/ipo/list` fetched at the same time (were sequential, up to 25 s each); an IPO-list failure no longer discards a good Hot Picks answer (`WATCHLIST_TIER1_IPO_OPTIONAL=0` restores strict); errors carry route + type (`/stockky-hot ReadTimeout`); the `Tier 1 (api-gateway) empty/unavailable` line now names the case (call failed / breaker open, cached copy empty, or gateway answered with no catalysts, the last at INFO). Tier fall-through unchanged. New `tests/test_group177_watchlist_tier1_reasons.py` (14 cases); sandbox: with the existing watchlist/afterhours/group 15x-16x suites 460 passed; details in `docs/GROUP177_WATCHLIST_TIER1_REASONS_AND_PARALLEL_FETCH.md`. Rebuild real-trade-service.
- 2026-10-06 (group 176)  news: HF 400 pauses itself, the five news sources fetched in parallel (analysis-intelligence-service).
  Item 5 of the open list (except Google-News-only). `news/main.py`: an HF 400/401/403/404/410 pauses sentiment calls for `HF_ERROR_BACKOFF_SEC` (1800 s, 0 = off) and logs ONE warning with HF's own message and the model, instead of one `HF API error: 400` per headline; to fix the 400 itself set `HF_MODEL` to a model your token can use. `event/main.py`: Yahoo/Google/Moneycontrol/ET/CNBC fetched at the same time instead of one after another (same merge order and dedupe; a raising source counts as empty); `EVENT_NEWS_PARALLEL=0` restores sequential. Tests added to `test_news_main.py` and `test_event_main.py` (news 166 passed; event 287 passed, 1 route-order test fails identically on the group 173 code in this sandbox); details in `docs/GROUP176_NEWS_HF_ERROR_PAUSE_AND_PARALLEL_SOURCES.md`. Rebuild analysis-intelligence-service.
- 2026-10-06 (group 175)  surprise/scan: one full-universe scan at a time (api-gateway).
  Item 9 of the open list. A scan past the 20 s deadline kept running in the background, but every later `/surprise/scan` request (Surprise tab, real-trade-service `cached=true` poll) started another full scan, so slow scans piled up and the stale answer got older (252 s in the log). `main.py`: while a default full-universe scan is in flight, later callers join it; `symbols` and `force_reload` calls are never shared; deadline/stale behaviour unchanged. `SURPRISE_SCAN_SINGLE_FLIGHT=0` restores one scan per request. Why a single scan exceeds 20 s is not touched. 7 new tests in `tests/test_main_surprise_routes.py`, run in the sandbox with the related gateway suites (244 passed); details in `docs/GROUP175_SURPRISE_SCAN_SINGLE_FLIGHT.md`. Rebuild api-gateway.
- 2026-10-06 (group 174)  log noise: ledger sync lines and the re-import skip line throttled.
  Item 8 of the open list (first two parts). `position-stocks-service/capital/ledger.py`: `ledger: synced from broker` and `sync_peer_pnl ... cached` (every ~13 s cycle) now log on change, else every `LEDGER_SYNC_LOG_EVERY_S` (300 s). `real-trade-service/portfolio/portfolio.py`: `skipping re-import of <symbol>` (LATENTVIEW, every cycle for up to 24 h) logs once per symbol/close, then every `RECENT_CLOSE_SKIP_LOG_EVERY_S` (1800 s). 0 = old behaviour; warnings/errors unchanged. The per-tab polling part is not changed (frontend freshness trade-off, needs your call). New `tests/test_group174_ledger_sync_log_throttle.py` and `tests/test_group174_recent_close_skip_log_throttle.py`, run in the sandbox together with the existing ledger and portfolio suites; details in `docs/GROUP174_LOG_NOISE_LEDGER_SYNC_AND_REIMPORT_SKIP.md`. Rebuild position-stocks-service and real-trade-service.
- 2026-10-06 (group 173)  boot burst: live feeds start once, on the real universe.
  Item 7 of the open list. At boot AngelOne + Yahoo feeds started on the ~250-symbol default universe and ~20 s later the first `/scan/universe` (~491 symbols) made the AngelOne feed stop, log in and subscribe a second time. `market-data-service/main.py`: with `API_GATEWAY_URL` set the startup hooks wait for the first universe and the refresh loop starts the feeds once; if none arrives within `FEED_BOOT_UNIVERSE_WAIT_S` (60 s) a fallback starts them on the default universe as before. `FEED_BOOT_WAIT_FOR_UNIVERSE=0` restores the old start. Second half of item 8 (pause state resets on restart) not done, unclear which pause. New `tests/test_group173_boot_feed_defer.py`; details in `docs/GROUP173_BOOT_FEEDS_START_ONCE.md`. Rebuild market-data-service.
- 2026-10-06 (group 172)  volume_shock: why daily history is missing, no re-asking for symbols that have none.
  Item 5 of the open list (item 11 closed with no change: `main.py:1597` is a fallback, `portfolio.py:529` prices a handful of imports). `candidate_engine/candidates.py`: `_fetch_history` records the failure reason per symbol (exception class, HTTP code, empty answer); the cycle WARNING lists the reasons; symbols that definitely have no history (404/400, empty, under 6 candles) are not requested again for `CANDIDATE_VOLUME_SHOCK_NOHIST_TTL_S` (6 h, 0 = off) and do not feed the market-data alarm. Timeouts/429/5xx are never paused. New `tests/test_group172_volume_shock_history_reasons.py`, `tests/conftest.py` resets the state; details in `docs/GROUP172_VOLUME_SHOCK_HISTORY_REASONS_AND_PAUSE.md`. Rebuild real-trade-service.
- 2026-10-06 (group 171)  held-symbol quote calls: bulk-first priority lane.
  Item 3 of the open list. The exit cycle sent `/live-quote` + `/quote` per open position (~10 requests for 5 positions every ~10 s, from four callers) and the large batch sent every bulk leftover through the same two calls (224 symbols). `market_feed/feed.py`: priority lane now makes ONE `POST /quotes/bulk` first (rows at most `FEED_PRIORITY_BULK_MAX_AGE_S`=10 s old, `FEED_PRIORITY_BULK_TIMEOUT_S`=4), per-symbol cascade only for symbols it missed; a failed bulk-first call pauses it for `FEED_PRIORITY_BULK_COOLDOWN_S`=30; priced held ticks are shared for `FEED_PRIORITY_SHARE_S`=3 s across callers and spellings; non-priority bulk leftovers skip `/live-quote` (only when bulk worked); the bulk log line now says why symbols were left. Off switches: `FEED_PRIORITY_BULK_FIRST=0`, `FEED_PRIORITY_SHARE_S=0`, `FEED_LEFTOVER_SKIP_LIVE=0`. New `tests/test_group171_held_quote_calls.py` (31 tests); details in `docs/GROUP171_HELD_SYMBOL_QUOTE_CALLS.md`. Rebuild real-trade-service.
- 2026-10-05 (group 170)  market-data overload: guarded calls from analysis-intelligence, readable timeout logs.
  Item 2 of the second open-market log list. `/analyze` fanned out to market-data (`/fundamentals`, `/history`, `/quote`) with no limit, so a slow market-data made every call wait out its full 35-60 s timeout and pile on. New `analysis-intelligence-service/md_guard.py` wraps those calls (technical + fundamental): at most `MD_MAX_CONCURRENT` (12) in flight, identical concurrent requests share one upstream call, and after `MD_BREAKER_THRESHOLD` (8) timeouts in a row calls fail fast for `MD_BREAKER_COOLDOWN_S` (15). No caching. `MD_GUARD=0` / `MD_BREAKER=0` turn it off. Failures use the existing fallbacks. Empty-message logs fixed: technical `history error`, fundamental `Unexpected error` (now with symbol), real-trade-service `dynamic_universe /check trigger failed ()`. New `tests/test_md_guard.py` (30 tests) plus wiring tests; not run in the sandbox (no pytest/httpx), guard logic checked against a stand-in httpx. Details: `docs/GROUP170_MARKET_DATA_CALL_GUARD_AND_READABLE_TIMEOUT_LOGS.md`. Rebuild analysis-intelligence-service and real-trade-service.
- 2026-10-05 (group 169)  Watchlist: penny and ETF rows retired, INFO lines folded into one summary (real-trade-service).
  Item 5 (second part) of the second open-market log list. Group 157 held penny/ETF rows back but left them active, so they were re-priced every cycle, and each logged its own `SKIPPED` line; the 30-minute throttle is per row and in memory, so after a boot and every 30 minutes all of them logged together (~450 INFO lines). ETF names are now retired before any price lookup and never inserted by `refresh_watchlist`; a price below `CANDIDATE_MIN_STOCK_PRICE` retires the row (`expired`, reason `instrument: ...`) and the group 158 cooldown (24 h) keeps it from being re-added. The per-row `SKIPPED` / `EXPIRED` lines became one INFO summary line per cycle (first 8 names + count; per-row detail at DEBUG). Tally keys unchanged; `instrument_expired` only when non-zero. `WATCHLIST_INSTRUMENT_RETIRE=0` restores the group 157 hold-back. New `tests/test_group169_watchlist_instrument_retire_summary_log.py` (19 tests, not run in the sandbox); details in `docs/GROUP169_WATCHLIST_PENNY_ETF_RETIRE_AND_SUMMARY_LOG.md`. Rebuild real-trade-service.
- 2026-10-05 (group 168)  Merge of the group 164 zip (watchlist / entry-precheck / IndianAPI line) and the group 167 zip (scalp-review line).
  The two zips branched from group 161, so group numbers 162-164 exist twice with different content (docs keep their own file names). Taken from the 164 zip: real-trade-service watchlist (`symbol_filter.py`, `watchlist.py`, `entry.py`), analysis-intelligence IndianAPI cooldown, position-stocks `tests/test_group163_entry_precheck.py`. Taken from the 167 zip: all other position-stocks changes (groups 162-167). Merged by hand in position-stocks `main.py`, `config.py` and `tests/test_main.py` (entry pre-check hunks added on top of the breakdown/repair routes and scalp-review settings). Not run: the sandbox has no pytest; run the three services' suites on the VM. Rebuild real-trade-service, analysis-intelligence-service and position-stocks-service.
- 2026-10-05 (group 167)  Read-only trade breakdown to settle the entry-window question (position-stocks-service).
  New `GET /trades/breakdown?days=N`: closed trades grouped by 30-minute IST entry bucket, scan window and exit status, with win rate, total/average P&L and average max gain/drawdown per group (`orders/review_stats.py`). No trading rule changed; the 09:30-14:30 window stays until the numbers (or your chosen times) say otherwise. +7 tests (2558 passed), module at 100%. Details: `docs/GROUP167_TRADES_BREAKDOWN_FOR_ENTRY_WINDOW_DECISION.md`. Rebuild position-stocks-service.
- 2026-10-05 (group 166)  Repair route reports the daily-loss limit; clock-dependent gate tests fixed (position-stocks-service).
  `POST /reconcile/repair-closed` now returns `daily_loss_pct`, `daily_loss_limit_pct`, `exceeds_daily_loss_limit` and `kill_switch_tripped` so you can see whether corrected P&L is past the daily-loss limit; it reports only and never trips the kill switch (use `POST /kill` if wanted). Separately, five `test_trade_gates.py` tests failed whenever the machine clock was just past IST midnight (also on the group 162 zip); the module now pins the clock to 12:00 IST. +3 tests, 2551 passed. Details: `docs/GROUP166_REPAIR_LOSS_LIMIT_REPORT_AND_CLOCK_PINNED_TESTS.md`. Rebuild position-stocks-service.
- 2026-10-05 (group 165)  Repair route also fixes exit prices of closed TARGET/STOP rows (position-stocks-service).
  Group 164 only corrected entry prices. `POST /reconcile/repair-closed` now also replaces the booked exit of TARGET_HIT/STOP_HIT rows with the real SELL fill from today's order book when exactly one order matches (own security id, TRADED, exact qty, within 10% of the booked exit); otherwise the exit stays. P&L, capital and ledger move by the combined difference; flat-SELL statuses are not re-read; an order-book failure still lets the entry repair run (`exit_error`). +9 tests; `reconcile.py`/`ledger.py` stay at 100%. Five wall-clock-dependent tests in `test_trade_gates.py` fail when the IST date has just rolled over (same on the group 162 zip); not related. Details: `docs/GROUP165_REPAIR_EXIT_FILLS_FOR_CLOSED_TARGET_STOP_ROWS.md`. Rebuild position-stocks-service.
- 2026-10-05 (group 164)  Repair today's closed scalp rows that carry a stale entry price (position-stocks-service).
  Group 162 only fixed trades from then on; rows already closed today (UNITEDPOLY: stored 44.58, filled 48.14) kept phantom P&L in the table and the ledger. New admin route `POST /reconcile/repair-closed` (dry run by default, `?apply=true` writes) re-reads the real entry fill from Dhan's super-order list, recomputes entry/capital/P&L from the stored exit price, and moves the ledger's cash, today's and lifetime P&L by the difference (`ledger.adjust_closed_pnl_today`). Idempotent; skips overnight partials, unmatched rows, no-fill rows and fills >25% off; exit prices and the kill switch are not touched. +10 tests (2537 passed); details in `docs/GROUP164_REPAIR_TODAYS_CLOSED_ROWS_ENTRY_PRICE.md`. Rebuild position-stocks-service.
- 2026-10-05 (group 163)  Exchange day-stats plausibility check (position-stocks-service).
  Group 162's day high/low/prev close come from unverified frame byte offsets. `feed/ws_client.py` now accepts a frame's values only if they are consistent with the last price (0.5x-2x of it, high >= low, price inside the range with 0.5% slack); otherwise they are ignored (previous good value kept), counted, and one WARNING is logged, and every consumer falls back to the tick buffer. `ws_status()` adds `day_stats_symbols/accepted/rejected` for a quick live check. No trading rule changed. +10 tests (2528 passed); details in `docs/GROUP163_DAY_STATS_PLAUSIBILITY_CHECK.md`. Rebuild position-stocks-service.
- 2026-10-05 (group 162)  Scalp review: P&L from real fills, entry price guard, exchange day range, faster exits (position-stocks-service).
  The day's 11 scalps showed +Rs105 on the dashboard but about -Rs24 at the broker; UNITEDPOLY alone was a phantom +124 (entry stored 44.58, filled 48.14). `reconcile.py` now takes the entry from the real ENTRY_LEG fill (parent filledQty / legDetails, not only parent TRADED), corrects it BEFORE flat-SELL exits compute P&L, and prefers the real SELL fill over a leg's static trigger price (unique-match guarded). `entry.py` rejects a stale tick, a live price >0.5% above the signal, or a stock up >7% on the day (`PRICE_GUARD:`). The range gate, adaptive levels and screener use the exchange's day high/low from the mode-3 frame instead of the 65-minute tick buffer. New no-follow-through exit (no +0.5% in 20 min), stagnation 45 -> 30 min, bar-ATR levels on by default, breakeven trigger capped at 1%, 1m and 15m windows paused (`DISABLED_SCAN_WINDOWS`). Rows already closed today keep their stale P&L. +100 tests (2513 passed); details in `docs/GROUP162_SCALP_REVIEW_FILL_ACCURACY_ENTRY_GUARDS.md`. Rebuild position-stocks-service.
- 2026-10-05 (group 164, watchlist line)  Watchlist: one decision per symbol per cycle, index names kept out (real-trade-service).
  Item 5 (first part) of the second open-market log list. A stock could hold several active rows (results / bulk_block / volume_shock), each with its own catalyst price, and the trigger pass judged each one separately: one QUEUED while another was SKIPPED in the same cycle (PACEDIGITK flapping). Also, NIFTY/BANKNIFTY/SENSEX items carry the index LEVEL as price, which became a row's catalyst price. New `watchlist_engine/symbol_filter.py`: index names are not inserted by `refresh_watchlist` and existing index rows are retired (`expired`, reason `index: ...`) by `evaluate_watchlist_entries`; per symbol only the best row (lowest tier, freshest catalyst, newest id; `.NS`/`.BO` = one stock) is evaluated, the others stay active and take over if it ends. Tally keys `index_expired` / `duplicates` only when non-zero. Env: `WATCHLIST_INDEX_FILTER=0`, `WATCHLIST_ONE_ROW_PER_SYMBOL=0` (off), `WATCHLIST_INDEX_SYMBOLS` (extra names). Note: a stock is now judged against its best catalyst only, so a weaker Tier-3 row can no longer queue it while the Tier-1 row is adverse. New `tests/test_group164_watchlist_one_row_per_symbol_index_filter.py` (49 tests); details in `docs/GROUP164_WATCHLIST_ONE_ROW_PER_SYMBOL_INDEX_FILTER.md`. Rebuild real-trade-service.
- 2026-10-05 (group 163, entry-precheck line)  Quality gate no longer runs for candidates that cannot be entered (position-stocks-service).
  Item 4 of the second open-market log list. SATIN and COMSYN were quality-gated (HTTP calls to analysis-intelligence) every ~13 s cycle although the pool could not afford one share, and the same happened whenever all position slots were used; `attempt_entry()` only found out afterwards. `_run_cycle` now ends with `MAX_POSITIONS_FULL` before the market filter and quality gate when open positions (OPEN + EXIT_LEGS_REJECTED) reach `MAX_CONCURRENT_SCALP_POSITIONS`, and drops candidates priced above `available_capital` before the top-N slice (next affordable ones move up; none left = `ALL_CANDIDATES_UNAFFORDABLE`, logged at most every 5 min). Both are exact lower bounds and fail open. `ENTRY_PRECHECK=0` restores the old order. New `tests/test_group163_entry_precheck.py` (25 tests); details in `docs/GROUP163_ENTRY_PRECHECK_BEFORE_QUALITY_GATE.md`. Rebuild position-stocks-service.
- 2026-10-05 (group 162, IndianAPI line)  IndianAPI 429 starts a cooldown instead of being hammered (analysis-intelligence-service).
  Item 3 of the second open-market log list. At the open VINCOFE, SATIN, KOHINOOR, COMSYN, KKCL and DCI each hit IndianAPI repeatedly and got 429 every time: `fundamental/indianapi_fallback.py` remembered nothing, so every call took a rate-limit slot, made a request and logged an ERROR. A 429 (status code or via `raise_for_status`) now pauses ALL IndianAPI requests for 120 s, doubling per 429 in a row up to 900 s (`Retry-After` honoured when longer, capped); other failures keep only that symbol out for 10 min; a success resets the streak. Fresh and stale cache are still served during a cooldown. One WARNING per 429 instead of an ERROR per symbol. Env: `INDIANAPI_COOLDOWN=0` (off), `INDIANAPI_COOLDOWN_S`, `INDIANAPI_COOLDOWN_MAX_S`, `INDIANAPI_SYMBOL_FAIL_TTL_S` (blank/invalid = default). New `tests/test_group162_indianapi_backoff.py` (30 tests); details in `docs/GROUP162_INDIANAPI_429_COOLDOWN.md`. Rebuild analysis-intelligence-service.
- 2026-10-05 (group 161)  Symbols with no price answer fast on `/quote`; the missing `/last-close` route added (market-data-service).
  Item 1 of the second open-market log list. BMISL, QUALIANCE, BAGMANE (and 3PLAND, MFML, AVALON, CMRGREEN, SUNLOC) are not live equities: each `/quote` walked the whole waterfall and could hold a worker for up to 18 s, so real-trade-service's 8 s read timed out every cycle (group 160 ignores timeouts by design). After 2 full-waterfall failures in a row `/quote` now answers "no price" at once (`source: negative_cache`) for 5 min, doubling to 1 h; a real price clears it, yfinance-cooldown failures do not count, a symbol with a last-good price is never cached. Also, `GET /last-close/{symbol}`, which real-trade-service's preview path calls, did not exist (every call was a 404); it now answers from cache / bhavcopy only. Env: `QUOTE_NEG_CACHE=0` (off), `QUOTE_NEG_AFTER`, `QUOTE_NEG_TTL_S`, `QUOTE_NEG_MAX_S`. New `tests/test_group161_quote_negative_cache.py`; details in `docs/GROUP161_QUOTE_NEGATIVE_CACHE_LAST_CLOSE_ROUTE.md`. Rebuild market-data-service.
- 2026-10-05 (group 160)  Symbols with no price are paused instead of retried every cycle (real-trade-service).
  Item 5 of the open-market log list. QUALIANCE, BMISL, BAGMANE, EMBASSY, ANNAPURNA and AAKASH answered 404 / "no price" every cycle, and each retry spent a `/live-quote` + `/quote` call (plus a bulk miss) from the rate-limit budget real symbols need. `market_feed/feed.py` now counts definite misses on `/quote` (HTTP 404, or 200 with no usable price); after 3 in a row the symbol is left out of non-priority batches for 30 min, doubling to a 6 h cap. Timeouts and 5xx do not count, a real price clears the count, open positions (priority lane) are never skipped. Env: `FEED_DEAD_SKIP=0` (off), `FEED_DEAD_AFTER_MISSES`, `FEED_DEAD_BACKOFF_S`, `FEED_DEAD_BACKOFF_MAX_S`. New `tests/test_group160_dead_symbol_pause.py`; details in `docs/GROUP160_DEAD_SYMBOL_PAUSE.md`.
- 2026-10-05 (group 159)  One spelling per stock in the price feed and the watchlist (real-trade-service).
  Item 4 of the open-market log list. `KOTAKBANK` and `KOTAKBANK.NS` were fetched as two symbols, and `ARE&M` / `M%26M` were keyed differently. `market_feed/feed.py`: `_clean_sym` now percent-decodes and strips only a trailing `.NS`/`.BO`; `get_quotes` fetches each distinct stock once and returns the tick under every spelling asked for; per-symbol URLs encode the symbol once (`M&M` -> `M%26M`). `watchlist_engine/watchlist.py`: new rows are stored under the clean symbol and the duplicate and cooldown checks match the suffixed spellings of older rows. New `tests/test_group159_symbol_canonical.py`; details in `docs/GROUP159_SYMBOL_CANONICAL_SPELLING.md`.
- 2026-10-05 (group 158)  Watchlist rows that fell far below their catalyst are retired, and not re-added at once (real-trade-service).
  Item 9 of the open-market log list. Group 155 stopped such rows being queued but left them `active` until `expires_at` (up to 36 days for "results"), so rows 4-45% under their catalyst were polled every cycle for nothing. `evaluate_watchlist_entries` now marks a row `expired` (reason starts `adverse:`) when it is more than `WATCHLIST_EXPIRE_DROP_PCT` (default 15%) below its catalyst; smaller falls still just stay active. `refresh_watchlist` does not re-insert that symbol+catalyst for `WATCHLIST_DROP_COOLDOWN_HOURS` (default 24), because a re-add would take the lower price as a new baseline. New `tests/test_group158_watchlist_deep_drop_expiry.py`; details in `docs/GROUP158_WATCHLIST_DEEP_DROP_EXPIRY.md`.
- 2026-10-05 (group 157)  Watchlist trigger holds back ETFs and penny stocks before they are queued (real-trade-service).
  Item 3 of the open-market log list. MASPTOP50 and MAFANG (ETFs) and low-priced names such as HARDWYN were queued and then cost a history fetch in `evaluate_mode` only to be rejected downstream. `entry.py` now skips a row whose price is below `CANDIDATE_MIN_STOCK_PRICE` (default 20) or whose symbol looks like an ETF (built-in list, `*BEES`/`*ETF` names, `WATCHLIST_ETF_SYMBOLS`). The row stays active, one `SKIPPED` INFO line per symbol per 30 min, counted in the existing `adverse` tally, off with `WATCHLIST_ADVERSE_GUARD=0`. New `tests/test_group157_watchlist_penny_etf_hold.py`; details in `docs/GROUP157_WATCHLIST_PENNY_ETF_HOLD.md`.
- 2026-10-05 (group 156)  Tier-3 direction guard now works for `live_quotes` ticks (real-trade-service).
  The group 155 day-change check needs the previous close, but the `/live-quote` (live_quotes) tick path never filled it, so for most symbols the guard failed open: the VM log showed 10 Tier-3 rows (SAKAR -5.7 %, SHANKARA -5.2 %, SPORTKING -6.2 % ...) queued at 0.00 %. `Tick.prev_close` is now read from `/live-quote`'s `ohlc.close` (AngelOne's previous-session close; missing, <= 0, or equal to the LTP is treated as unknown and stays fail-open). +10 tests (2 fail on the group 155 code). Rebuild real-trade-service. Details: `docs/GROUP156_LIVE_QUOTES_TICK_PREV_CLOSE_TIER3_GUARD.md`.
- 2026-10-05 (group 155)  Watchlist trigger: no falling knives, Tier-3 direction check (real-trade-service).
  Items A3/A4/A5 of the open-market log audit. The trigger only rejected an upward overrun of the entry band, so stocks down 7-20% since their catalyst (KMSUGAR, GLOTTIS, BAJAJHCARE...) were queued, and Tier 3 (raw momentum-movers incl. decliners) had no direction check. A row is now not queued (stays active) when it is more than `WATCHLIST_MAX_DROP_PCT` (3 %) below its catalyst, or, for Tier 3, when its day change vs previous close is below `WATCHLIST_TIER3_MIN_DAY_CHANGE_PCT` (+1.0 %); fail-open without a previous close; `WATCHLIST_ADVERSE_GUARD=0` restores the old behaviour. `Tick` gains an optional `prev_close`. The 0.00 % moves are the by-design Tier-3 first-tick baseline. +13 tests. Rebuild real-trade-service. Details: `docs/GROUP155_WATCHLIST_ADVERSE_MOVE_AND_TIER3_DIRECTION_GUARD.md`.
- 2026-10-05 (group 154)  Volume-shock: time-of-day volume projection, quote pre-check before history, per-cycle history-missing warning (real-trade-service).
  Item 5 of the open-market VM log. The last daily candle is today's partial session but was divided by a 20-day average of full sessions, so a 2x-volume stock showed ~0.4x at 10:00 IST and was rejected; partial volume is now projected with an approximate intraday cumulative-volume curve (floored at 15 %, `VOLUME_SHOCK_TOD_ADJUST=0` restores the raw ratio). The quote is fetched first and a mover whose live return is clearly below the return gate (1-point margin) is rejected without a history request (`CANDIDATE_VOLUME_SHOCK_QUOTE_PREFILTER=0` restores always-fetch). One WARNING per cycle counts symbols whose history was unavailable. +21 tests (all error on the group153 code); `candidates.py` stays 100%. Rebuild real-trade-service. Details: `docs/GROUP154_VOLUME_SHOCK_TIME_OF_DAY_VOLUME_QUOTE_PREFILTER_HISTORY_WARNING.md`.
- 2026-10-05 (group 153)  Market-data overload: AngelOne ticks written in batches, api-gateway prices large lists bulk-first (market-data-service, api-gateway).
  Item 4 of the open-market VM log. The AngelOne feed wrote `live_quotes` one row per transaction (~491 sequential DB round trips per poll cycle, on the polling thread), which on a slow Oracle link stretches a cycle past the 20 s freshness window and sends every position/candidate to the slow per-symbol `/quote` route; the AngelOne calls themselves were already paced. Rows of one AngelOne batch now go in ONE transaction off the event loop (491 writes -> 10 per cycle; `ANGELONE_FEED_DB_BATCH=0` restores the old path), `feed_status()` reports `last_cycle_s`, and a cycle over 15 s logs a rate-limited WARNING. In api-gateway, `_fetch_prices_bulk_async` (hot-picks pass 2, scan chunks) de-duplicates symbols and prices lists of 15+ with chunked `POST /quotes/bulk` first (stale rows ignored, 8 s cache), per-symbol only for the misses (`GATEWAY_BULK_QUOTE_MIN=0` turns it off). Poll interval unchanged. +9 market-data tests (8 fail on the old code), +11 gateway tests (all fail on the old code); market-data suite 769 passed, api-gateway files pass one process per file. Rebuild market-data-service and api-gateway. Details: `docs/GROUP153_MARKET_DATA_OVERLOAD_BATCHED_TICK_WRITES_BULK_FIRST_GATEWAY_PRICES.md`.
- 2026-10-05 (group 152)  Fundamentals route restored, raw-feed route order, priority quotes for open positions (analysis-intelligence-service, real-trade-service).
  Open-market VM log: (1) `GET /fundamental/analyze/<sym>` returned the string "SYM.NS" because the group112 helper `_md_fundamentals_symbol` sat under the `@app.get("/analyze/{symbol}")` decorator, which broke every `.get()` on the answer (real-trade market-cap floor, 14 symbols); decorator moved back onto `analyze()`. (2) `/events/raw-feed` was shadowed by `/events/{symbol}` (logged as RAW-FEED.NS), so watchlist Tier 2 was always empty; route moved above it. (3) The 5 open positions' `/live-quote` + `/quote` calls timed out every exit cycle while a ~923-symbol one-request-per-symbol batch hit its 45 s deadline; `get_quotes(priority=True)` now uses its own pool, 2x timeouts and a `/quotes/bulk` fallback, and large batches are priced bulk-first with a 20 s staleness guard. +17 tests (5 route tests fail on the old code). Rebuild analysis-intelligence-service and real-trade-service. Details: `docs/GROUP152_FUNDAMENTAL_ROUTE_RAWFEED_ORDER_PRIORITY_QUOTES.md`.
- 2026-10-05 (group 151)  After-hours scan no longer picks everyday words (IT, OIL, ACE) as stocks (real-trade-service).
  The 2026-10-05 scan kept `IT`, `OIL`, `ACE` in its top 8: real tickers, but also the sector word, "oil prices" and a plain word. The known-universe path of `_extract_symbol` ignored the stopword list and returned the first token in the symbol master. Now sector/generic words are skipped (the scan tries the next token, so "IT stocks: TCS jumps" gives TCS) and OIL/ACE resolve only by company name (Oil India, Action Construction). Already-stored rows are not deleted. +13 tests (5 fail on the old code), real-trade suite 2733 passed (3 modules not importable in my sandbox). Also checked the duplicate AngelOne feed thread: already handled in code, no change. Rebuild real-trade-service. Details: `docs/GROUP151_AFTERHOURS_WORD_TICKERS.md`.
- 2026-10-05 (group 150)  Intraday news tick no longer fails with ORA-00908 on Oracle (real-trade-service).
  After the group 149 redeploy the VM log showed `intraday news tick failed` with `WHERE ...afterhours_news_scan_enabled IS 1`: `_intraday_news_body` used `.is_(True)`, which Oracle (no native BOOLEAN) renders as `IS 1`; SQLite accepts it so the tests never caught it. Now `== True` (`= 1`); it was the only such filter in `services/`. +4 tests (Oracle-dialect compile check + a guard against `.is_(True/False)`), real-trade suite 2718 passed (3 modules not importable in my sandbox). Rebuild real-trade-service. Details: `docs/GROUP150_INTRADAY_NEWS_ORACLE_BOOLEAN_FILTER.md`.
- 2026-10-05 (group 149)  Published ports bound to 127.0.0.1; host and container nginx hardened against scanners (deploy config only, no service code).
  VM logs showed bots probing `/.env`, `phpunit eval-stdin.php`, `/containers/json`, and api-gateway logging `Invalid HTTP request received`: compose published every container port on all interfaces (Docker bypasses ufw). All 8 ports now `127.0.0.1:...` (host nginx already proxies to loopback); container nginx returns 404 for dotfiles/config extensions instead of the SPA `index.html`; host nginx adds `server_tokens off`, basic security headers, `return 444` for probe paths (certbot `/.well-known/` kept) and a per-IP rate limit on `/` only. New `scripts/check_compose_ports_loopback.py`. Tested under nginx 1.24 here; not on the VM. Run `./deploy/deploy.sh`. Also check the Oracle security list allows only 22/80/443. Details: `docs/GROUP149_LOCK_DOWN_PORTS_AND_NGINX_HARDENING.md`.
- 2026-10-04 (group 148)  AngelOne feed "scrip master not loaded yet" is a WARNING for the first 3 attempts, ERROR only if it drags on (market-data-service).
  From the 8 h VM log audit: this self-resolving boot condition was logged at ERROR on every start. New `_scrip_wait_log_level(attempt)`; retry/backoff unchanged. Other audit lines reviewed, no code change (AngelOne 403 rate limit already has a cooldown; NSE 403 shares the VM-IP block with Yahoo, still 429). +3 tests, market-data suite 760 passed. Rebuild market-data-service. Details: `docs/GROUP148_SCRIP_MASTER_WAIT_LOG_LEVEL.md`.
- 2026-10-04 (group 147)  Feed-universe refresh warning now names the error (market-data-service); item 6 closed.
  From the VM log audit: `feed universe refresh: fetch failed, keeping existing feed:` ended with nothing because httpx timeouts have an empty `str()`. New `_exc_detail` logs the exception type (and message when present); behaviour unchanged. Other audit lines reviewed: no code change (NSE 403 / bhavcopy and IPO static fallbacks share the VM-IP block with Yahoo). Item 6 closed: Yahoo 429s the VM's IPv4, no IPv6 route. +3 tests, market-data suite 757 passed. Rebuild market-data-service. Details: `docs/GROUP147_FEED_UNIVERSE_ERROR_DETAIL.md`.
- 2026-10-04 (group 146)  `/training/api/insights` no longer returns invented insights (decision-prediction-service).
  `get_learning_insights` returned three hard-coded examples (sample sizes 124/87/65) when a training report existed in the working directory, and a 404 otherwise, so the Training tab could show made-up statistics as learned results. Nothing computes real insights yet, so it now returns an empty list (the tab already shows "No insights available yet."). Items 9 (remainder) and 12 closed as keep-as-is. +4 tests, training suite 42 passed. Rebuild decision-prediction-service. Details: `docs/GROUP146_LEARNING_INSIGHTS_NO_PLACEHOLDERS.md`.
- 2026-10-04 (group 145)  Repair-seeded PE now carries the seed flag the feed merge reads (api-gateway); remaining list corrected (item 25 was already closed).
  `/api/feed/repair` seeded a baseline PE of 22.5 and flagged it `pe_seed`, but `merge_feed_payload` only protects against the flag `pe_ratio_seed` (what bulk writes), so a repair seed was not recognised as a seed and could overwrite a real stored PE in a race. Repair now sets both flags. +1 test, 1 extended. No trading path reads these flags. The previous two remaining lists wrongly kept item 25 open: group 114 closed it (values kept, reviewed 2026-10-04) and the boot log confirms it. Rebuild api-gateway.
- 2026-10-04 (group 144)  Log audit (fresh VM startup + scan logs): shared cache for the site-wide news feeds; Gemini "no text" no longer a warning.
  The event tracker downloaded Moneycontrol, Economic Times and CNBC TV18 again for EVERY symbol (3 identical downloads per symbol; CNBC TV18 returned 0 entries every time). They are now fetched once per 5 min (empty or failed: 10 min), thread-safe, `EVENT_FEED_CACHE_SECONDS=0` restores the old behaviour. Separately, a Gemini 200 reply with no text part logged `Gemini call failed: KeyError('parts')`; it now logs the finishReason and uses the template note. +11 tests event, +8 prediction. Rebuild analysis-intelligence-service and decision-prediction-service.
- 2026-10-04 (group 143)  Weekday NSE holiday skips real-trade-service schedule tick (no pre-pick / eDIS notification).
  `_schedule_tick` only checked Mon-Fri, so on a weekday holiday (Fri 2026-10-02, Tue 2026-10-20) the pre-pick and eDIS morning check ("market need not be open" jobs) still ran and notified for a day with no session. Now skipped on a holiday; lookup failure keeps the old answer. Enter-at-open, EOD square-off and EOD scan were already guarded by `is_market_open_ist`. +3 tests, 1 updated. Rebuild real-trade-service.
- 2026-10-04 (group 142)  Tooling: the holiday-list sync script now covers 2027+ and reminds you when a year runs out.
  It only compared 2026 dates, so 2027 dates added to one of the seven copies and forgotten in another would never be reported, and on 1 Jan 2027 every service would silently treat holidays as trading days. Now compares every date from 2026 on and prints a NOTE (exit 0) when the current or next year has no dates (from 1 Oct) - it prints that today. No 2027 dates added (no official calendar). +10 tests (28 pass in the file under a stand-in runner). No service code changed. Details: `docs/GROUP142_HOLIDAY_SYNC_SCRIPT_ALL_YEARS.md`.
- 2026-10-04 (group 141)  Scan-universe cache TTL treats an NSE holiday as closed (api-gateway).
  `_build_scan_universe` used weekday + hours only, so on a weekday holiday the universe was cached 30 min instead of 6 h and rebuilt all day. Now 6 h on a holiday; lookup failure keeps the old answer. The other weekday-only checks group 140 listed were reviewed: surprise_scanner and data_feed already check holidays, the rest are safe by design (see the doc). Test table row for Fri 2026-10-02 corrected (it was a holiday), +1 test; not run under pytest here, real TTL block verified by extraction. Rebuild api-gateway. Details: `docs/GROUP141_SCAN_UNIVERSE_TTL_HOLIDAY.md`.
- 2026-10-04 (group 140)  NSE holiday awareness in three more places (analysis-intelligence technical, decision, position-stocks WS).
  Same gap as group 139: the technical cache TTL, the decision cache TTL and the position AngelOne WebSocket idle check only looked at weekday + hours, so on a weekday holiday (Fri 2026-10-02, Tue 2026-10-20) they behaved as open. Each now returns closed/idle on a holiday (off switch and lookup failure keep the old behaviour). `check_holiday_lists_sync.py` now checks seven copies. 9 new tests; the decision ones run here, the technical and position ones could not (no httpx/numpy/websockets) - function bodies verified by extraction. Rebuild those three services. Details: `docs/GROUP140_HOLIDAY_AWARENESS_TECHNICAL_DECISION_POSITION_FEED.md`.
- 2026-10-04 (group 139)  market-data-service now knows NSE holidays (market-data-service).
  It had no holiday check, so on a weekday holiday (Fri 2026-10-02) the AngelOne/Yahoo feeds still polled 09:05-15:35 and quote/history caches used the 5-15 minute open-session TTLs. New `_NSE_HOLIDAYS_2026` + `is_nse_holiday_ist()` in `market_hours.py`; `is_feed_window_ist()` and `main.is_market_open()` return closed on a holiday (always-on switch still wins; lookup failure = old behaviour). `scripts/check_holiday_lists_sync.py` now checks five copies. 18 new tests + one test-helper base-week fix; run here under a stand-in runner only (no pytest/fastapi). Rebuild market-data-service. Details: `docs/GROUP139_MARKET_DATA_NSE_HOLIDAY_AWARENESS.md`.
- 2026-10-04 (group 138)  Closed market: `scan(cached=true)` serves a result computed after the last close instead of re-sweeping (api-gateway).
  The boot no longer swept when closed, but every later cached call age-rejected the restored result (~220 s) and ran the full ~1000-quote sweep again. Now, on weekends/holidays/outside 08:30-15:30 IST, a result at least 600 s past the last trading-day close is served (`market_closed_cache: true`); pre-open stays live; per-symbol requests excluded; `SURPRISE_CLOSED_MARKET_CACHE=0` turns it off. 12 new tests, api-gateway suite 8208 passed. Rebuild api-gateway. Details: `docs/GROUP138_CLOSED_MARKET_CACHED_SCAN_SERVES_POST_CLOSE_RESULT.md`.
- 2026-10-04 (group 137)  An early `scan(cached=True)` call can no longer delete the saved last result (api-gateway).
  `scan(cached=True)` loaded the durable last result with the plain kv read, which DELETES an expired row, so a caller arriving before the closed-market boot warm (20 s after start) destroyed the only copy and forced the full quote sweep. It now uses the stale-aware loader; the age check still decides freshness. 2 new tests (real kv_cache + SQL table; both fail on the old line), api-gateway suite 8196 passed. Rebuild api-gateway. Details: `docs/GROUP137_EARLY_CACHED_SCAN_KEEPS_LAST_RESULT.md`.
- 2026-10-04 (group 136)  AngelOne token fallback also accepts NSE `-BZ` rows (market-data-service).
  The group 135 report confirmed the `-BE` fix (unresolved list: 5 names -> `SHREETNB, WARDINMOBI`). WARDINMOBI exists only as `WARDINMOBI-BZ` (NSE "permitted to trade"), so `-BZ` is now a last-resort tier: `-EQ` > `-BE` > `-BZ`; movers sweep still `-EQ` only. SHREETNB has no AngelOne row at all (Yahoo fallback handles it). 2 new tests, suite 726 passed. Rebuild market-data-service. Details: `docs/GROUP136_ANGELONE_BZ_FALLBACK_TIER.md`.
- 2026-10-04 (group 135)  AngelOne token lookup falls back to NSE `-BE` rows (market-data-service).
  HFCL, MTARTECH and STLTECH exist in AngelOne's scrip master only as `-BE` rows, so the feed/quote/history paths never resolved them and used Yahoo. `get_token`/`get_tokens_bulk` now try `-EQ`, then a `-BE` fallback built for names with no `-EQ` row; `get_all_symbols` (movers sweep) stays EQ-only; position-stocks-service untouched. Disk snapshot carries the BE map (old snapshots still load). 5 new tests, market-data suite 724 passed. Rebuild market-data-service. Details: `docs/GROUP135_ANGELONE_BE_SERIES_FALLBACK.md`.
- 2026-10-04 (group 134)  Tests: repo-wide source sweeps no longer scan a virtualenv inside `services/` (api-gateway tests).
  The VM run failed 2 of 8194 api-gateway tests (`test_env_flag_sweep`, `test_env_numeric_sweep`) because `real-trade-service/.venv/.../site-packages` (pandas, coverage) was scanned as Stockky code. Five tree walkers now also skip `.venv`, `venv`, `site-packages`, `.tox`, `build`, `dist`. Reproduced here with a fake in-tree venv; no application code changed. Also records the subshell loop for running all four suites. Details: `docs/GROUP134_SWEEP_TESTS_SKIP_VENVS.md`.
- 2026-10-04 (group 133)  Tooling: `scripts/diagnose_group132.sh`, a read-only one-command report for the two items that need the VM.
  Shows whether the closed-market boot restore fired and how many `/quote` calls followed the boot, the durable last-result row and its expiry, and every AngelOne scrip-master row (segment/series) for HFCL, MTARTECH, STLTECH or any names you pass. No application code changed, nothing to rebuild. Details: `docs/GROUP133_DIAGNOSE_SCRIPT.md`.
- 2026-10-04 (group 132)  Closed-market boot restore: the saved surprise result was deleted by the read meant to restore it (api-gateway).
  Group 131's caveat was right: `kv_cache`'s plain read DELETEs an expired row, and `_warm_surprise_scan_cache` ran the plain loader before the stale one, so the row was gone before `get_stale` looked. Reproduced against the real `kv_cache` + a SQL table. Now the closed branch does one stale read first (deletes nothing), and the last result is saved for 7 days (`SURPRISE_LAST_RESULT_TTL_SEC`, floor = old ~340 s; freshness is still age-checked at read time).
  6 new real-kv tests + 3 updated/added; all four service suites run for real in the sandbox this time and pass. Details: `docs/GROUP132_LAST_RESULT_SURVIVES_EXPIRY.md`.
- 2026-10-04 (group 131)  Closed-market boot warm now actually skips the sweep (api-gateway).
  Group 120's skip restored the last surprise result with a TTL-respecting read, but that result is saved with a ~340 s TTL, so overnight nothing was restored and the full quote sweep ran (the post-deploy boot log printed "pre-warmed", not "restored ... skipped"). New `_load_last_result_stale_from_durable_cache` (get_stale) is tried in the closed/holiday branch; freshness is still age-checked at read time.
  Also corrects my earlier "burst looks gone" reading (logs can't show it). 6 new tests (loader and warm branch run standalone here, not under pytest). Details: `docs/GROUP131_CLOSED_MARKET_WARM_STALE_RESTORE.md`.
- 2026-10-04 (group 130)  After-hours scan: the group 121 split line is logged only when rows were written (real-trade-service).
  The post-deploy boot log printed it right before group 85's "0 rows written - <why>" line, duplicating it. Zero-write passes now log just the "0 rows written" line + funnel. No behaviour change.
  Tests adjusted/added (function run against stubs here, not under pytest). Details: `docs/GROUP130_AFTERHOURS_NO_DUPLICATE_SUMMARY.md`.
- 2026-10-04 (group 129)  News fetch logging: one INFO summary per symbol instead of 7+ per-source lines (analysis-intelligence-service).
  `_fetch_headlines` now logs `news sources for SYM: yahoo_news=0 google_news=1 ... (total N)`; per-source lines are DEBUG, failures stay WARNING. No behaviour change.
  1 new test (loop run against stubs here, not under pytest). Details: `docs/GROUP129_NEWS_SOURCE_LOG_SUMMARY.md`.
- 2026-10-04 (group 128)  Hugging Face news sentiment moved to the Inference Providers router (analysis-intelligence-service).
  The old `api-inference.huggingface.co` host no longer resolves, so every headline fell back to neutral 0.0. `_score_headline` now posts chat-completions to `router.huggingface.co` (env `HF_MODEL`, `HF_API_URL`), and skips the call for 300 s after a network failure. Unverified: whether the default model is served to your token; see the doc for what to set.
  Tests updated/added (function run against stub httpx here, not under pytest). Details: `docs/GROUP128_HF_SENTIMENT_ROUTER_MIGRATION.md`.
- 2026-10-04 (group 127)  AngelOne `/angelone/movers`: rate-limited sweeps are flagged (market-data-service).
  Post-deploy boot log: AngelOne 403 "exceeding access rate" mid-sweep, yet `status=ok, quotes_fetched=2610 of 2710` was cached for the full TTL. Now under 98 % coverage adds `partial: true` + `missing_quotes`, logs a WARNING and caches 120 s so the next poll retries; complete sweeps unchanged.
  5 new tests (helper run standalone, not under pytest). Log findings with no code change (unresolved HFCL/MTARTECH/STLTECH etc. are real `-EQ`-less names; Hugging Face `api-inference` endpoint retired so sentiment is always neutral): `docs/GROUP127_ANGELONE_MOVERS_PARTIAL_SWEEP.md`.
- 2026-10-04 (group 126)  Guard test: the built-in 250-symbol boot universe has no known-delisted symbol (market-data-service).
  Checked the real list (no AAKASH/ANNAPURNA/TATAMTRDVR, no duplicates or blanks) and pinned it with 2 tests. No production change. The "248/250" unresolved symbols are other names; group124's warning will list them.
  Details: `docs/GROUP126_FALLBACK_UNIVERSE_GUARD.md`.
- 2026-10-04 (group 125)  Feed universe drops known-delisted symbols (market-data-service).
  `_refresh_feed_universe_loop` now passes api-gateway's `/scan/universe` through `_clean_feed_universe`, which skips names in `KNOWN_DELISTED_SYMBOLS` (AAKASH, ANNAPURNA, TATAMTRDVR) so neither WS feed subscribes to them; logged once per change.
  5 new tests (helper run standalone here, not under pytest). Details: `docs/GROUP125_FEED_UNIVERSE_DROPS_DELISTED.md`.
- 2026-10-04 (group 124)  AngelOne feed "resolved 248/250 requested symbols" warning now lists the unresolved names (market-data-service).
  `angelone_ws_feed.py` appends `; unresolved: A, B (+N more)` (max 20, normalised like the scrip master). Reporting only; ticks and the Yahoo fallback are unchanged.
  5 new tests (run standalone here, not under pytest). Details: `docs/GROUP124_ANGELONE_FEED_UNRESOLVED_NAMES.md`.
- 2026-10-04 (group 123)  yfinance "Failed to create TzCache folder ... File exists" x3 at boot (market-data-service).
  Already fixed in `main.py` (dir pre-created with `exist_ok=True` before `set_tz_cache_location`); the pasted log predates it and the fix had no entry or test. Added 2 source-level guard tests and this note; no production change. Rebuild to see the effect.
  Details: `docs/GROUP123_TZ_CACHE_DIR_PINNED.md`.
- 2026-10-04 (group 122)  Log-audit item: "no last-known movers cached yet" printed 4x per pass (api-gateway).
  The three Movers routes and the momentum collector all hit the same empty branch of `_get_nifty50_data()`. The line is now INFO once per phase per 600 s, DEBUG after; return values unchanged.
  The gateway's "No supported WebSocket library" warning was not changed (requirements already have `uvicorn[standard]`; likely a stale image, check command in the doc).
  1 new test (unrun here: no pytest/fastapi; branch run against stubs). Details: `docs/GROUP122_MOVERS_EMPTY_LOG_THROTTLE.md`.
- 2026-10-04 (group 121)  Log-audit item: after-hours "10 symbol(s) scored -> 1 rows upserted" (real-trade-service).
  Not lost symbols: after a restart the earlier pass's rows are already stored, so only the 1 new symbol was written. The summary line now says
  `N scored -> W new/updated, U already stored at an equal or higher score (unchanged), F failed`, and the Telegram header adds `· U already stored`.
  No write/scoring/notification-rule change. 3 new tests (unrun here: no pytest/sqlalchemy; function run against stubs). Details: `docs/GROUP121_AFTERHOURS_ALREADY_STORED_LOG.md`.
- 2026-10-04 (group 120)  Log-audit item: startup quote burst (api-gateway).
  The boot pre-warm ran a full ~1,000-symbol surprise quote sweep immediately on every start. It now waits `SURPRISE_BOOT_WARM_DELAY_SEC` (default 20, 0 = old), skips the sweep when the market is closed and a saved result exists, and otherwise reuses a fresh saved result (`cached=True`).
  Details: `docs/GROUP120_STARTUP_QUOTE_BURST.md`.
- 2026-10-04 (group 119)  Log-audit item: "Neon" wording in logs and status text on the Oracle deployment (api-gateway, market-data, notification-scheduler, decision-prediction).
  Runtime messages that run on both databases (scan status lines, KV fallbacks, bhavcopy progress, keep-alive loop) now say "DB"/"durable DB"; Neon-only branches keep their wording. No behaviour change.
  Details: `docs/GROUP119_DB_NEUTRAL_WORDING.md`.
- 2026-10-04 (group 118)  Log-audit item: AAKASH/ANNAPURNA still 404'd after group 99 (api-gateway + market-data).
  The surprise static-cache loaders and `/quotes/bulk`, `/history`, `/fundamentals` had no delisted check (only `/quote` did); all now skip or fast-fail.
  Details: `docs/GROUP118_DELISTED_404_FAST_PATHS.md`.
- 2026-10-04 (group 114)  Log-audit item 25: owner reviewed the six regime constants, kept every value, and had the review dates moved to 2026-10-04 (real-trade-service + notification-scheduler-service).
  No value changed, so trading behaviour is unchanged; the "stale regime constant" warnings stop until the dates are 30 days old again (2026-11-03). The scheduler's two copies and real-trade-service's
  `_REGIME_CONSTANTS` must now agree on dates, guarded by two new drift tests. Tests unrun here (no pytest/sqlalchemy in sandbox). Doc: `docs/GROUP114_REGIME_CONSTANTS_REVIEWED.md`.
- 2026-10-04 (group 113)  Log-audit item 6 tooling: `scripts/diagnose_yfinance.sh`, a read-only one-command report for the yfinance-returns-nothing problem on the VM.
  Prints DNS, Yahoo chart status with a default and a browser user-agent, a yfinance probe inside the api-gateway container and the recent yfinance/Movers log lines. No application code
  changed, nothing to rebuild. The report is what item 6 has been waiting for. Doc: `docs/GROUP113_YFINANCE_DIAGNOSTIC_SCRIPT.md`.
- 2026-10-04 (group 112)  Log-audit item 4 leftover: `analyze()` and the decision service asked market-data for `/fundamentals/INFY`, the peer step for `INFY.NS` (analysis-intelligence + decision-prediction).
  A plain NSE ticker is now requested as `.NS` in both places (same canonical form as the peer step); indices, names with spaces, `^` symbols and already-suffixed symbols are passed through unchanged. market-data
  already normalised its own cache key, so this changes URL spelling in logs only, no Yahoo calls. Tests unrun here (no pytest/fastapi/httpx in sandbox). Doc: `docs/GROUP112_FUNDAMENTALS_CANONICAL_URL.md`.
- 2026-10-04 (group 111)  Movers stale label now says when the last-known list was saved (api-gateway + frontend).
  The last-known list is stamped with its save time (new key `stockky:market_movers_last_known_at`, 7-day TTL, durable via the existing prefix rule). Stale rows carry
  `stale_since`, the three Movers routes add `stale_since` beside `stale`, and the panel note reads "(saved YYYY-MM-DD HH:MM IST)". A missing or bad stamp just falls back to
  the group110 wording. Tests unrun here (no pytest/fastapi in sandbox). Doc: `docs/GROUP111_MOVERS_STALE_SINCE.md`.
- 2026-10-04 (group 110)  Movers panel marks last-known rows as stale (api-gateway + frontend).
  When the panel serves the last-known list (pre-open/closed, or open session with yfinance and AngelOne both empty), each row now carries `stale: true`, the
  `/market/top-gainers`, `/top-losers` and `/most-active` responses add `stale: true`, and the panel shows a one-line "last known list" note. Fresh and AngelOne rows are
  unchanged. Tests unrun here (no pytest/fastapi in sandbox). Doc: `docs/GROUP110_MOVERS_STALE_LABEL.md`.
- 2026-10-04 (group 109)  Log-audit item 20: Movers panel no longer goes empty when yfinance returns nothing during the open session (api-gateway).
  The panel never used NSE; it used a yfinance fetch with no open-session fallback. It now falls back to AngelOne whole-market movers (5%+ only, no volume, so Most Active stays
  empty), then to the last-known list. Fallback rows are not cached. api-gateway: 8123 passed. Doc: `docs/GROUP109_MOVERS_OPEN_SESSION_FALLBACK.md`.
- 2026-10-04 (group 108)  Log-audit item 9: Yahoo news is skipped for 10 minutes after 10 empty/failed lookups in a row (analysis-intelligence news).
  Saves a wasted yfinance call per symbol; the other headline sources are untouched, so results are unchanged. Tunable with `YAHOO_NEWS_BACKOFF_AFTER` (0 = off) and
  `YAHOO_NEWS_BACKOFF_SECONDS`. A replacement news source is still the owner's call. analysis-intelligence: 2167 passed. Doc: `docs/GROUP108_YAHOO_NEWS_BACKOFF.md`.
- 2026-10-04 (group 107)  Scheduler's stale-constant warning named `ENTRY_REGIME_MIN_SCORE=38`; real-trade-service has used 25 since 2026-09-03.
  Both scheduler copies now show 25, and a new drift test compares their values with real-trade-service's config defaults. Review dates deliberately not moved (item 25
  still needs the owner's review). notification-scheduler: 181 passed. Doc: `docs/GROUP107_SCHEDULER_REGIME_CONSTANT_VALUES.md`.
- 2026-10-04 (group 106)  `technical_thin` is now visible: amber `THIN TECH` tag on scan rows and a strip on Hot Picks conviction cards (frontend only).
  Label only, nothing filtered or scored by it; absent/false shows nothing. `tsc --noEmit` clean; no frontend test runner; not viewed in a browser.
  Doc: `docs/GROUP106_TECHNICAL_THIN_BADGE.md`.
- 2026-10-04 (group 105)  Log-audit item 12 remainder: label-only `technical_thin` on scan and Hot Picks rows (api-gateway).
  Same test as the `/stock/{symbol}` "Technicals on minimal price history" flag (group94), now set as a bool on full-analysis scan rows and carried into Hot Picks cards.
  No score, decision or ranking change; fast-path/error rows get no field; no frontend display yet. Tests: 7 new in `test_main_analyze_symbol.py`. api-gateway: 8118 passed.
  Doc: `docs/GROUP105_TECHNICAL_THIN_LABEL.md`.
- 2026-10-04 (group 104)  Test fix: `test_telegram_long_message_split` expected 2-4 parts for a 22,279-character message, which correctly splits into 6 (3,800 per part).
  The splitter was right (parts under 4,096, joined text identical); the test's bound was a miscount and failed since it was written. It now derives the expected
  count from the splitter's own limit. No production code changed. notification-scheduler: 178 passed. Doc: `docs/GROUP104_TELEGRAM_SPLIT_TEST.md`.
- 2026-10-04 (group 103)  kv_cache pool-settings audit: a blank pool env value aborted Oracle engine creation (all services).
  `oracle_compat.oracle_engine_kwargs` did int() on values that `os.getenv` returns as "" when a variable is set but empty (`CACHE_DB_POOL_SIZE_ORACLE=`), raising
  ValueError; kv_cache's Oracle branch also skipped its shared-variable fallback on a blank. Blank now means unset (override -> env var -> default) in all 8 identical
  `oracle_compat.py` copies and 6 `kv_cache.py` copies; a real non-numeric typo still raises. Defaults unchanged. Tests: api-gateway `test_oracle_pool_blank_env.py` (10).
  Suites: gateway 8111, market-data 690, analysis 2161, real-trade 2948, decision 38, prediction 20, training 38 passed; position-stocks and notification-scheduler each
  have one failure that also fails on the untouched code (dhanhq not installed here; a Telegram long-message split test). Doc: `docs/GROUP103_ORACLE_POOL_BLANK_ENV.md`.
- 2026-10-04 (group 102)  Log-audit item 29 remainder: REAL pollers ran while armed but logged out (frontend).
  The Pipeline (2 s), Watchlist (5 s) and Positions (10 s) pollers and the one-shot positions/orders/candidates load were allowed to run when the gate was only
  armed, but REAL positions/orders/candidates/pipeline are admin-only on the server regardless of arming, so every call was a 401. They now need a login session
  in REAL (DEMO unchanged). Auto-pilot is server-side and unaffected. `tsc --noEmit` clean; no frontend test runner. Doc: `docs/GROUP102_ARMED_LOGGED_OUT_POLLERS.md`.
- 2026-10-04 (group 101)  Remaining log-audit items 7, 10, 11, 23, 27, 28 (api-gateway, market-data, analysis-intelligence, decision-prediction).
  Item 7: the leftover `/health?warm=true` wake pings (gateway history + deep keepalive, decision, prediction, training) now follow the `WAKE_PINGS`/`ORACLE_DSN`
  rule. Item 23: `_waterfall_nse_direct_price` and `_fetch_nse_fundamentals` share group96's quote-equity pause. Items 11/28: the stored searched-symbols list
  cleans itself on read (delisted, non-equity, junk, curated typo aliases) and is written back once. Item 10: a payload with no sector/industry compares against no
  peers instead of the generic big-cap list. Item 27: premarket baselines with no symbols use the live scan universe once loaded. Item 19 reviewed, no change needed. Full suites: gateway 8101, analysis-intelligence 2161, market-data 690,
  decision 38, prediction 20, training 38 passed. Doc: `docs/GROUP101_REMAINING_FIXES.md`.
- 2026-10-04 (group 100)  Log-audit item 29: protected endpoints called before login caused 401 bursts (frontend).
  The Pipeline tab's `CatalystWatchlistPanel` polled admin-only `/resilience/status` every 30 s with or without a session (and the REAL watchlist when logged
  out), and one shared `Promise.all` meant that 401 also blanked the DEMO watchlist. It now asks only for what the session may read (resilience with a token,
  REAL watchlist with a token, DEMO watchlist always), settles each result separately, and reloads on login. `rtRequest` and the backend are unchanged; other
  default-auth callers were not changed (some routes are admin-only only in REAL mode). `tsc --noEmit` clean; no frontend test runner exists. Doc: `docs/GROUP100_PIPELINE_PANEL_401_BURSTS.md`.
- 2026-10-04 (group 99)  Log-audit item 28: delisted symbols (AAKASH, ANNAPURNA) in the Hot Picks universe returned 404 on every run (api-gateway).
  `stockky_hot_stocks` merged movers/news/events/scan seeds/watchlist/searched/IPOs with no delisted or non-equity gate (the scan universe has one). It now drops
  `KNOWN_DELISTED` and non-equity symbols after de-duplication and logs one line with the names; nothing is renamed and the nifty fallback is unchanged. Stored
  watchlist/searched entries are not purged. Also fixed a group93 test (`test_symbol_alias_table_is_consistent_with_extra_new_symbols`) that failed on the new
  HEROMOTOCO aliases; no production change. Full api-gateway suite run here: 8090 passed. Doc: `docs/GROUP99_HOTPICKS_DEAD_SYMBOLS.md`.
- 2026-10-04 (group 98)  Log-audit item 27: "SURPRISE_UNIVERSE/SCAN_UNIVERSE not set ... live universe build returned nothing" (market-data-service).
  Unset is a supported setup: the built-in 250-symbol list is only the boot-time universe, and the feed-universe refresh loop swaps in api-gateway's live
  `/scan/universe` about 20 s after boot. The warning was inaccurate (nothing tries a live build there) and repeated by every caller. Now logged once per process
  with accurate text; returned list unchanged; not set in docker-compose on purpose. Premarket baselines run with no symbols still use the built-in list.
  Full market-data-service suite run here: 677 passed. Doc: `docs/GROUP98_FALLBACK_UNIVERSE_LOG.md`.
- 2026-10-04 (group 97)  Log-audit item 24: stale DEMO open-positions snapshot (snap_as_of 2026-09-21) re-reported on every boot (real-trade-service).
  The snapshot is only written by a running cycle, so a mode with the gate disarmed / auto-pilot off (DEMO) froze it, and `reconcile_on_startup` re-raised the
  same RECONCILE_MISMATCH on every boot. Now a reported mismatch re-baselines that mode's snapshot from the live OPEN/PARTIALLY_CLOSED rows (DB untouched,
  matching modes untouched, a failed re-save is non-fatal). Also fixed: a MANUAL after-hours scan that scored nothing sent no Telegram reply, contrary to the
  group95 note (found by the first real pytest run of group95's tests). Full real-trade-service suite run here: 2948 passed, 1 skipped.
  Doc: `docs/GROUP97_STALE_DEMO_SNAPSHOT.md`.
- 2026-10-04 (group 96)  Log-audit item 23: NSE `quote-equity` returns 403 on delivery lookups (market-data-service).
  `bhavcopy.delivery_from_quote()` returned None silently on every 403 and kept calling NSE for each symbol before falling back to bhavcopy. Now a
  401/403/429 pauses the quote path for `NSE_QUOTE_BLOCK_SECONDS` (default 600, 0 = off): no NSE call during the pause, the cached cookie session is dropped,
  one warning is logged when the pause starts, and a 200 ends it early. Delivery values/sources unchanged. `_waterfall_nse_direct_price` and
  `_fetch_nse_fundamentals` in main.py are not on the pause; the 403 itself (datacenter IP) is not fixed. Full market-data-service suite run here: 673 passed.
  Doc: `docs/GROUP96_NSE_QUOTE_403_PAUSE.md`.
- 2026-10-04 (group 95)  Log-audit item 19: Telegram messages on every boot and every after-hours scan (real-trade-service).
  A scheduled after-hours scan messaged on every tick even with nothing new (and once per mode right after each boot); finalize sent two messages for the
  same shortlist; the stale-regime-constants notice went out on every boot. Now: scheduled scans message only when rows were written (manual runs always
  reply; a save-failure pass is reported once per mode/date/count), the duplicate finalize message is removed, and the stale notice is sent when the stale set
  changes or at most weekly (`STALE_NOTICE_INTERVAL_HOURS`, state file `STALE_NOTICE_STATE_PATH`, default /tmp so a container re-create sends one).
  Other Telegram senders were not reviewed; item 25 (the stale constants themselves) is unchanged. Not run under real pytest in the sandbox (helpers checked
  standalone, files compiled). Doc: `docs/GROUP95_TELEGRAM_NOISE.md`.
- 2026-10-04 (group 94)  Log-audit item 12: no UI note when technical analysis ran on the minimal bar.
  The technical service already writes "Limited history..." / "Fallback quote..." reasons for a one-bar or fallback history, but `/stock/{symbol}` never
  turned them into a data-quality flag, so the card could say "Data quality: high" over a neutral 50 from one bar. `api-gateway/main.py` now adds the flag
  `Technicals on minimal price history` (first in the list, level high -> medium) when `reasons.technical` carries a thin/fallback marker or the decision
  pillar map says technical is not live. Scoring and the frontend are unchanged; other entry points (scan, hot picks, real-trade) do not show it.
  Not run under real pytest in the sandbox (helper checked standalone, files compiled). Doc: `docs/GROUP94_THIN_TECHNICAL_HISTORY_NOTE.md`.
- 2026-10-04 (group 93)  Log-audit item 11: a mistyped name ("HERO MOTERS") silently resolved to HEROMOTORS, which is not an NSE symbol.
  `api-gateway/main.py` `/stock/{symbol}` recorded every typed name as "searched" before analysing it, and the searched list feeds the known-symbol set, so
  typos became fuzzy targets (HEROMOTERS scores 0.90 vs HEROMOTORS, 0.70 vs HEROMOTOCO). Now: spaces removed, fuzzy corrections applied silently only at
  >= 0.90 and 0.05 clear of the runner-up (otherwise 404 "Did you mean: ..."), HEROMOTORS-style spellings aliased to HEROMOTOCO, and a symbol is recorded as
  searched only after an analysis with a real close. Entries already in `stockky:searched_symbols` are not cleaned. Not run under real pytest in the sandbox
  (helpers checked standalone, tests compiled). Doc: `docs/GROUP93_SYMBOL_DID_YOU_MEAN.md`.
- 2026-10-04 (group 92)  Log-audit item 10: wrong peer sets (an auto maker got FMCG peers, a utility got IT peers, a retailer the generic list).
  `analysis-intelligence-service/fundamental/peer_multi_quarter.py::detect_sector` matched substrings (`"IT"` inside UTILITIES/CAPITAL; any "CONSUMER" -> FMCG,
  which is where Yahoo files autos and retail) and read `sector` before the more specific `industry`. Now: whole-word matching, `industry` -> `sector` ->
  `sectorDisp`, Consumer Cyclical is not FMCG, new `POWER` and `RETAIL` peer sets, non-string values ignored. Unknown sectors still use `DEFAULT`.
  The three named symbols' payloads were not seen (causes are the likely ones, not confirmed); `HEROMOTORS` is not an NSE symbol (item 11). New
  `TestDetectSectorWordMatching` (39 cases, 20 fail on the old code). Real pytest in a clean venv: peer tests 231 passed, full analysis-intelligence-service
  suite 2148 passed. Doc: `docs/GROUP92_PEER_SECTOR_WORD_MATCHING.md`.
- 2026-10-04 (group 91)  Log-audit item 31: `ledger.sync_from_broker` (and `equity_sync`) warned "verify this is the tradeable balance" for Dhan's `availabelBalance`.
  Dhan's fundlimit docs call that field "Available amount to trade", so the warning was stale for it; the real risk is a silent fallback to
  `sodLimit` (start-of-day, overstates intraday), `withdrawableBalance` (can understate) or `availableCash` (undocumented). Both services now log
  INFO for the documented keys and a WARNING naming the key, the previous key and what it is only for a fallback. Fallback chain and sizing unchanged.
  ledger also distinguishes "field present but <= 0" from "no field". Which key your account populates is not known from here; the log will show it.
  Tests updated/added in `test_ledger_coverage.py` and `test_equity_sync_remaining_coverage.py`; real pytest in clean venvs: position-stocks-service
  2412 passed, real-trade-service 2922 passed + 1 skipped. Doc: `docs/GROUP91_DHAN_BALANCE_KEY_LOGGING.md`.
- 2026-10-04 (group 90)  Log-audit item 30: the position-stocks log printed the first 8 characters of the AngelOne feed token on every login.
  `position-stocks-service/feed/angelone_session.py::_login` now logs `feed_token=received` / `feed_token=MISSING` and nothing of the value.
  Sweep of all services for other truncated-secret slices found only `notification-scheduler-service`'s `_mask`, which feeds the settings UI,
  not a log, so it is unchanged. The shared `SESSION_SECRET` between real-trade-service and position-stocks-service is NOT changed: it is the
  design for one admin login across both (risk and the larger alternative are in the doc). 1 new test function (3 cases), fails on the old code;
  real pytest in a clean venv: position-stocks-service 2406 passed. Doc: `docs/GROUP90_ANGELONE_FEED_TOKEN_LOG.md`.
- 2026-10-04 (group 89)  Log-audit item 5: the gateway had no Gemini cooldown, so every `/stock` call fired a Gemini request that 429'd.
  `api-gateway/main.py::_generate_ai_summary` now starts a cooldown on a 429 (`GEMINI_COOLDOWN_SECONDS`, default 600; a `Retry-After`
  header wins, clamped 30..3600 s; a later 429 never shortens it) and, while it runs, returns the Hinglish template with no request and
  no log line. One warning + one `rate_limit_monitor` event per cooldown window. Other non-200s and exceptions behave as before.
  Per-process (a restart clears it); decision-prediction-service keeps its own cooldown. 7 new test functions (14 cases) in
  `tests/test_main_scoring.py`, existing 429 test updated, fixture resets the state. Real pytest in a clean venv: full api-gateway suite
  8047 passed. Not run against live Gemini. Doc: `docs/GROUP89_GATEWAY_GEMINI_COOLDOWN.md`.
- 2026-10-04 (group 88)  Log-audit item 4: peer fundamentals fetched twice per analysis, as `INFY` and `INFY.NS`.
  `analysis-intelligence-service/fundamental/peer_multi_quarter.py`: the in-process fundamentals cache was keyed by whatever
  spelling the caller used and the market-data URL used that spelling too, so the two spellings were two cache entries and
  two HTTP calls. Now (1) cache get/set key on the canonical `.NS`/`.BO` form (`_norm_symbol`), (2) `fetch_fundamentals`
  always requests the canonical symbol, (3) a per-symbol lock makes concurrent asks for the same symbol one call (the others
  wait and read the cache), (4) `fetch_fundamentals_batch` fetches once when a list holds both spellings and returns both
  keys. `.BO` is kept distinct from `.NS`. Failed fetches are still not cached. NOT changed: `fundamental/main.py::analyze()` and
  `decision-prediction-service/prediction/main.py::_fetch_fundamentals` still call market-data with the bare symbol (market-data
  normalises its own cache key, so Yahoo is not hit twice; it is only a second cheap HTTP call). New
  `tests/test_peer_fundamentals_canonical_key.py` (9 tests) passes under a stand-in runner, as do the 98 in
  `test_peer_multi_quarter.py` and 80 of 85 in `test_peer_ranking.py` (the 5 are stand-in limits: caplog / parametrize forms);
  not run under real pytest/httpx. Doc: `docs/GROUP88_PEER_FUNDAMENTALS_CANONICAL_KEY.md`.
- 2026-10-04 (group 87)  Log-audit item 14: the browser `/ws` loop (1-5 requests/s answered `200 483`). `frontend/src/useRealtime.ts::
  toWsUrl` replaced the whole URL path with `/ws`, so with `VITE_API_URL=https://host/api` the socket went to `wss://host/ws`.
  The host nginx only proxies/upgrades WebSockets under `/api/`, so `/ws` fell through to the frontend container (index.html,
  483 bytes), the handshake failed and the client retried; live quotes fell back to polling. The base path is now kept
  (`https://host/api` -> `wss://host/api/ws`; nginx strips `/api/`, the gateway sees `/ws`; a bare host still gives `/ws`).
  Reconnect back-off: after 6 failed attempts in a row without ever opening, retries slow from 15 s to 2 min. `toWsUrl` is now
  exported. Checked the URL building in node for 7 inputs; no frontend test runner exists in the repo, so nothing added under
  `frontend/`; no `npm run build` was run. Doc: `docs/GROUP87_WS_URL_KEEP_API_PREFIX.md`.
- 2026-10-04 (group 86)  Log-audit items 22 and 26. (22) api-gateway `_get_all_nse_securities`: "Fetched 0 securities from NSE"
  was followed by "NSE live API unreachable" even when NSE answered HTTP 200 with no rows. It now says "answered but returned 0
  securities rows" when a response came back and keeps "unreachable" for no response; the bhavcopy fallback itself is unchanged.
  (26) market-data `angelone_ws_feed.py`: "thread did not stop within 10.0s" on every off-hours restart. Cause: the off-hours idle
  wait was one 60 s sleep, longer than the 10 s join. It now sleeps in 1 s slices. A thread that still outlived the join was
  re-enabled by the next start (shared `_running` flag) so two feed threads could poll at once, and its `finally` cleared the flag
  under the new one; a per-start generation number now makes the old thread exit and keeps it from touching the flag or writing
  ticks. `main.py` runs `stop_feed_background` via `asyncio.to_thread` so the join no longer blocks the event loop. New
  `market-data-service/tests/test_angelone_feed_single_thread.py` (4 tests, run under a stand-in runner: pass; not run under real
  pytest) and 3 new cases in `api-gateway/tests/test_main_universe.py` (unrun, no fastapi/pytest in sandbox). Doc:
  `docs/GROUP86_NSE_EMPTY_ROWS_FEED_SINGLE_THREAD.md`.
- 2026-10-04 (group 85)  Log-audit item 17: the real-trade-service after-hours scan (`watchlist_engine/afterhours_scan.py::
  run_afterhours_scan`) could finish with 0 rows written and no explanation. It now keeps a per-feed funnel (items fetched /
  stale / no NSE symbol / score <= 0 / scored, plus bulk/block-deal hits) and logs a plain-English reason whenever a pass
  writes 0 rows: every feed empty, items all dropped (with the counts per reason, and a note if the symbol master was
  unavailable), all scored symbols already stored at an equal or higher score, or upserts FAILED. Upsert failures are now
  counted, and the Telegram "No new rows" line says so when some failed to save. No scoring or DB-write behaviour changed.
  New `tests/test_afterhours_zero_row_explained.py` (unrun here: no pytest/sqlalchemy in the sandbox; the function was run
  against stubs and printed the expected lines). Items 22 and 26 not done (their text was not in the zip). Doc:
  `docs/GROUP85_AFTERHOURS_ZERO_ROW_EXPLAINED.md`.
- 2026-10-04 (group 81)  Log-audit item 1: the 20 s `/decide` timeout (and no overall deadline) in `api-gateway/main.py::
  get_stock_decision` (`GET /stock/{symbol}`). The first analysis after a boot (cold caches, competing with the start-up
  warm-ups and the hot-picks scan) overran the hard 20 s read timeout, the gateway returned a neutral HOLD, and the decision
  service's finished work was thrown away. Now: (1) decide read timeout `STOCK_DECIDE_TIMEOUT_SEC` (default 60 s);
  (2) one retry on `ReadTimeout` only, WITHOUT `force` (so it can use the downstream caches the first attempt warmed, or a
  decision that finished meanwhile), only if >= `STOCK_DECIDE_RETRY_MIN_SEC` (15 s) of the deadline is left; (3) an overall
  deadline `STOCK_OVERALL_DEADLINE_SEC` (default 100 s, under the browser's 120 s timeout): every call's timeout is clamped
  to the time left, the enrichment fan-out uses `asyncio.wait` so finished calls are kept and slow ones cancelled (was
  `gather`, which could outlive the budget), and Gemini is skipped (template) with < 3 s left or cut off at the deadline;
  (4) a timeout that survives the retry now returns an honest HOLD ("taking longer than usual ... try again in a minute",
  flag "Decision engine slow", error "decision engine timed out after Ns") instead of claiming the service is unreachable.
  Env values are blank/garbage/out-of-range safe (`_stock_budget_sec`). Documented in `.env.oracle.example`. Tests
  (`api-gateway/tests/test_main_stock_scan_routes.py`, 187 -> 210): 14 new in `TestStockTimeBudget` (defaults, env parsing,
  retry without force, no retry when budget is short / for non-read-timeouts, honest timeout HOLD, call timeouts clamped,
  slow enrichment dropped while finished kept, Gemini skipped / cut off); 3 existing tests rewritten (the 20 s pin, the
  `gather`-slot defensive test, the ReadTimeout-is-"unreachable" parametrisation). 24 of the new/rewritten fail on the
  group 80 code. Whole api-gateway suite ran to completion with no failure here (`-x`). NEEDS REBUILD: `docker compose build
  api-gateway && docker compose up -d api-gateway`. NOT FIXED here: the `/health?warm=true` and decision-side timeouts
  (`_HTTP_TIMEOUT` 45 s in decision/main.py) are unchanged; the frontend still retries `/stock` up to 3x on abort.
- 2026-10-04 (group 80)  Log-audit item 2: `ensure_oracle_identity` + "Oracle engine ready" on every `/training-score` call
  (twice per stock analysis). Three causes, all in `decision-prediction-service/training`: (1) `models.py::ensure_oracle_identity`
  had no once-per-engine guard, so each `ModelRegistry()` re-ran 2 lookups (+ DDL) for 8 tables; now a clean pass is final per
  engine and a pass with any failed table is retried at most every 300 s (body moved to `_ensure_oracle_identity_impl`, returns
  True/False). (2) `get_engine()` stored the REWRITTEN url (`oracle+oracledb://`, discrete ORACLE_* vars) but compared the
  un-rewritten one, so the singleton never hit on the Oracle VM: every call built a new engine/pool and logged "Oracle
  Autonomous DB engine ready"; the cache key is now the pre-rewrite url. (3) `app.py::/training-score` built a new
  `TrainingScanner` per request (model load + unpickle); now `_get_training_scanner()` shares one, rebuilt every
  `TRAINING_SCANNER_TTL_SEC` (default 120) and dropped on `promote_model`. `ModelRegistry()` also runs `create_all` once per
  engine (`_create_all_once`). Tests: NEW `training/tests/test_identity_once_and_engine_cache.py` (8) and
  `test_scanner_cache.py` (4); the engine-singleton case builds 3 engines on the group 79 code, 1 now. Training suite here:
  37 passed, 1 failed (`test_oracle_first_db_url.py::test_app_still_reports_sqlite_and_postgres_without_oracle`, fails the
  same way on the group 79 zip, not touched). decision/ suite 22 passed, 9 skipped. prediction/ suite not run (errored at
  collection on the sandbox, unrelated). NEEDS REBUILD: `docker compose build decision-prediction-service && docker compose
  up -d decision-prediction-service`. Check: after the first call, `/training-score` should no longer log the identity lines.
  NOT FIXED here: `train.py` / `scanner.py` still build their own `ModelRegistry()` (rare paths, now cheap).
- 2026-10-04 (group 79)  Log-audit item 8: market sentiment was always 50 in the decision engine. `docker-compose.yml`
  never set `API_GATEWAY_URL` for `decision-prediction-service`, so `decision/main.py` fell back to its hard-coded
  `https://api-gateway-puwd.onrender.com` (dead Render host); every `/market/indices` call failed and the neutral 50
  fallback was used for every stock. Set `API_GATEWAY_URL: http://api-gateway:8000` for `decision-prediction-service`,
  and for the two other services with the same silent Render fallback: `position-stocks-service` (`config.py` builds
  `MARKET_INDICES_URL` from it) and `analysis-intelligence-service` (`rate_limit_report.py` only posts events when it is
  set). No Python code changed. New `scripts/check_compose_gateway_url.py` (2 tests, runs with plain python or pytest):
  fails on the previous compose for those 3 services, passes now. NEEDS RECREATE (env change, no rebuild):
  `docker compose up -d decision-prediction-service position-stocks-service analysis-intelligence-service`.
  Check afterwards: decision log should show "Market sentiment fetched from API Gateway: <score>" instead of
  "All market sentiment fetches failed, using neutral 50". NOT FIXED here: the onrender defaults in code (harmless once
  compose sets the var), and the other audit items (next by priority: #2 `ensure_oracle_identity` on every request).
- 2026-10-03 (group 55)  #13 item (continued from group 54): the same `weight > capacity` flaw in the four other token
  buckets, all fixed the same way. Tokens are capped at `capacity`, so `tokens >= weight` can never become true for a
  heavier weight; a plain `acquire()` slept out the whole `max_wait` on every call and then "proceeded anyway".
  Files: `market-data-service/rate_limiter.py` (default `max_wait` 20 s; `acquire`, which also covers the fail-closed
  `try_acquire`, plus `would_block`, which was True forever for an oversized weight), `analysis-intelligence-service/
  fundamental/rate_limiter.py` and `notification-scheduler-service/scheduler/rate_limiter.py` (`acquire`), and
  `api-gateway/redis_rate_limit.py` (`acquire`, `allow`, `wait_budget_sec`; `main.py::_cb_get` uses the last two to
  decide on CircuitOpenError). An oversized weight now means "the whole bucket": it waits only for the bucket to fill
  (at most `capacity / rps`), drains it, and returns; `allow()` grants it when the bucket is full; `wait_budget_sec()`
  reports the time to fill; `would_block()` is False when the bucket is full. Logged once per call at DEBUG
  ("weight X exceeds bucket capacity Y, treating as Y"). Weight at or below capacity and `capacity == 0` are
  untouched. These copies have no reserve concept, so the clamp is simply `capacity`. Real call site affected:
  `market-data-service/surprise_premarket.py` `rl_acquire("yfinance", weight=len(batch))` (batches of up to 50).
  Tests: 5 existing tests that pinned the stall as "current behaviour" were rewritten (analysis-intelligence
  `test_max_wait_exceeded_proceeds_and_drains_tokens` now reaches the give-up path with an ordinary weight and
  `test_weight_above_burst_capacity_always_stalls_the_full_max_wait` became `..._is_capped_and_does_not_stall`;
  api-gateway `test_redis_rate_limit.py` `test_weight_larger_than_capacity_stalls_until_max_wait`, `test_weighted_allow`
  and `test_weight_forwarded`). New tests: market-data `tests/test_rate_limiter.py` 42 -> 53 (`TestOversizedWeight`);
  analysis-intelligence `tests/test_rate_limiter.py` 89 -> 95; api-gateway `tests/test_redis_rate_limit.py` 71 -> 82;
  NEW FILE `notification-scheduler-service/tests/test_rate_limiter_oversized_weight.py` (9; that service had no bucket
  tests, the file is loaded fresh from its path like `test_rate_limiter_env.py`). On the group 54 code 22 of the new/
  rewritten tests fail (market-data 7, analysis 4, redis 6, notification-scheduler 5); the group 54 versions of the
  three edited test files fail only on the 5 tests that were rewritten. Sandbox has no pytest and no network, so a small
  stand-in runner was used (fixtures, parametrize, raises, monkeypatch, caplog): these four files plus the unchanged
  `api-gateway/tests/test_rate_limiter.py` (160) all pass; other test files and the other tests in these services were
  NOT run (market-data `test_rate_limiter_fail_closed.py` is plain functions + real threads, uses weight 1, not run).
  Run each service's `bash run_tests.sh` / pytest on the VM. ZIP NOW HAS 831 ENTRIES (one new test file). NEEDS REBUILD of
  api-gateway, market-data-service, analysis-intelligence-service and notification-scheduler-service (`docker compose
  build <service> && docker compose up -d`).
  STILL OPEN: a caller whose weight is above `capacity - reserve` but not above `capacity` is still denied/stalled in the
  gateway's reserve-aware bucket (`test_unreachable_weight_under_reserve_is_denied` pins it as intended); needs your call.
- 2026-10-03 (group 54)  #13 item: `api-gateway/rate_limiter.py::_Bucket.acquire(weight > capacity)` stalled for the
  whole budget on every call. Tokens are capped at `capacity` (`min(capacity, tokens + elapsed * rps)`), so
  `usable >= weight` can never become true for a weight above it. A plain `acquire()` therefore slept out the full
  budget (5 s by default) and then "proceeded anyway", draining the bucket and bumping `denied_events`; `try_acquire()`
  (fail_fast) waited the same budget and then returned False, i.e. the call was skipped on every attempt. The one real
  call site is `surprise_premarket.py::bulk_baselines_from_yfinance`, which does
  `rl_acquire("yfinance", weight=len(batch))` with `SURPRISE_YF_BULK_BATCH=50` against a 6-token bucket. Fixed in
  `acquire()`: when `weight > capacity` the request is treated as a request for everything the caller may take,
  `need = capacity - reserve` (so a background pipeline still leaves the interactive reserve alone). It waits only
  for that to refill (at most `capacity / rps`, e.g. 3 s for yfinance), drains it, and returns. Logged once per call at
  DEBUG. If nothing is attainable (`reserve >= capacity`) behaviour is unchanged, and a weight at or below capacity is
  untouched. The give-up path and its log line still report the caller's original weight. `snapshot()` output is
  unchanged (no new counter). Tests (`tests/test_rate_limiter.py`, 149 -> 160): 11 new in `TestBucketAcquire`
  (full bucket taken immediately, waits for refill not `max_wait`, just-above-capacity, fail_fast granted instead of
  skipped, reserve left intact, reserve + refill wait, DEBUG-only log, still bounded by the budget when nothing refills,
  reserve >= capacity unchanged, weight == capacity not treated as oversized, module `acquire`/`try_acquire`); 8 of
  the 11 fail on the group 53 code, the other 3 guard behaviour that must not change. All 149 existing tests still
  pass unchanged on the new code. Sandbox has no pytest and no network, so this round used a small stand-in runner
  that supports fixtures (autouse/yield), `parametrize`, `raises` and `monkeypatch`: the whole file runs 160/160 on the
  fix, 152 pass / 8 fail on the old code. The rest of the gateway suite was NOT re-run (7530 passed in group 51); the
  other test files that mention the limiter stub it. Run `bash run_tests.sh` on the VM. NEEDS REBUILD:
  `docker compose build api-gateway && docker compose up -d`.
  STILL OPEN, same flaw, NOT changed: (1) the sibling copies of this bucket in market-data-service/rate_limiter.py,
  analysis-intelligence-service/fundamental/rate_limiter.py and notification-scheduler-service/scheduler/rate_limiter.py
  (default `max_wait` 20 s, and market-data-service/surprise_premarket.py also calls
  `rl_acquire("yfinance", weight=len(batch))`), plus `api-gateway/redis_rate_limit.py`'s `_Bucket` (acquire / allow /
  wait_budget_sec). (2) A background caller whose weight is above `capacity - reserve` but not above `capacity` is
  still denied/stalled: `test_unreachable_weight_under_reserve_is_denied` pins that as intended, so I left it for your
  call.
- 2026-10-03 (group 53)  #13 item: `api-gateway/json_safe.py::sanitize` left `np.bool_` and `set` / `frozenset`
  unchanged. `sanitize()` is what `_safe_json_response` runs every payload through before Starlette's
  `json.dumps(..., allow_nan=False)`; anything it returns that `json.dumps` cannot encode becomes a 500 for the
  whole response. `np.bool_` is not a `bool` subclass and was not in the numpy branch (which only handled
  floating / integer / ndarray), so a flag such as `np.float64(3) > np.float64(2)` in a row raised
  `TypeError: Object of type bool is not JSON serializable`; a `set` value (tag sets, id sets) raised
  `Object of type set is not JSON serializable`. Fixed: `np.bool_` -> `bool(obj)` in the numpy branch; `set` /
  `frozenset` -> list, sorted for a stable wire order (falls back to iteration order when members are not mutually
  orderable, e.g. `{1, "a", None}`), members sanitised recursively (so a NaN member becomes null and numpy ints
  become ints). Arrays of bools already worked via `tolist()`. Only `json_safe.py` changed; the training service's
  own `json_safe.py` already handled both and is untouched. Tests (`tests/test_json_safe.py`, 32 -> 42): new
  `TestNumpyBool` (4) and `TestSets` (6); 10 of the 42 fail on the group 52 code. Sandbox has no pytest this round
  (and the network is off), so the file was run through a minimal pytest shim (parametrize / approx /
  monkeypatch.setattr only): 42/42 pass on the fix, 32 pass / 10 fail on the old code. The whole gateway suite was
  NOT re-run (7530 passed in group 51); `test_main_core` / `test_main_surprise_routes` only reference `json_safe`
  through stubs, so nothing else pins the old behaviour. Run `bash run_tests.sh` on the VM. NEEDS REBUILD:
  `docker compose build api-gateway && docker compose up -d`.
- 2026-10-03 (group 52)  #13 item: `api-gateway/price_resolver.py::_as_positive_float` accepted `+inf`. The guard
  was `px <= 0 or px != px`, which rejects zero, negatives and NaN but lets `+inf` through (also the strings
  `"inf"`, `"Infinity"` and `"1e400"`, which `float()` turns into inf). `extract_safe_price` / `resolve_display_price`
  / `apply_price_aliases` / `ensure_row_price` would then stamp `inf` into close/price/cmp/current_price/ltp/
  last_price/prev_close, and from there into scoring, % change and order sizing. The guard is now
  `not math.isfinite(px) or px <= 0`, so an infinite value is skipped like any other bad one: the resolver falls
  through to the next key / source, and returns 0.0 (or leaves the row untouched) when nothing finite is available.
  The old test `test_infinity_is_accepted_as_positive` pinned the bug and is replaced by
  `test_infinity_and_nan_rejected` (+/-inf, inf strings, 1e400, nan) and `test_large_finite_still_accepted`; new
  class `TestInfinityNeverResolves` (8 tests) covers fall-through across tick/decision/feed/nested feed, the
  only-inf-available case, `apply_price_aliases` (inf price keeps an existing positive value; inf `prev_close` is
  replaced) and `ensure_row_price`. 13 of the 72 tests in `tests/test_price_resolver.py` fail on the group 51 code.
  Sandbox has no pytest this round, so this file was run through a minimal pytest shim (parametrize/approx only):
  72/72 pass on the fix, 13 fail on the old code; I did NOT re-run the whole gateway suite (7530 passed in
  group 51). Run `bash run_tests.sh` on the VM. Only `price_resolver.py` and its test file changed, so the other
  callers (`instant_scanner`, `main.py`) are untouched. NEEDS REBUILD: `docker compose build api-gateway &&
  docker compose up -d`.
- 2026-10-03 (group 51)  #13 item: `GET /ops/rate-limits` (and its `/api/rate-limits` and `/api/ops/rate-limits`
  aliases) blocked the event loop. `RateLimitMonitor.snapshot()` is blocking: it does a durable `kv_get` of the
  Neon/Oracle aggregate (`stockky:rate_limit_stats`) and, when Redis is enabled, an `lrange`. The route is
  `async def` and called it inline, so every dashboard poll froze the whole gateway for a DB round trip while other
  requests waited. `record()` had already been moved to a thread pool for exactly this reason; `snapshot()` was the
  read-side leftover. `main.py::ops_rate_limits` now does `circuits = all_snapshots()` on the loop (in-memory, cheap)
  and `await asyncio.to_thread(rate_limit_monitor.snapshot, circuits=circuits)`. `snapshot()` itself is unchanged
  apart from a docstring saying it is blocking and must be called off-loop. It has no other caller. Tests
  (`tests/test_main_ops_routes.py::TestCircuitsAndMetrics`, 211 total in the file): the snapshot runs on a thread
  with no running event loop and not the loop's thread; a 0.3 s blocking snapshot leaves the loop free to tick at
  least 10 times (it ticks ~0-1 inline); the circuits are read on the loop and handed over as the same object; a
  snapshot error still propagates. 2 of the 4 fail on the group 50 code (the other 2 pin behaviour that must not
  change). Sandbox now has the gateway's pinned requirements (fastapi 0.111, httpx 0.27, pydantic 2.11, numpy 2.2 /
  pandas 3.0 for yfinance), so this round ran the WHOLE gateway suite in one process: 7530 passed, which also
  re-verifies the group 47-50 changes together. NEEDS REBUILD: `docker compose build api-gateway && docker compose
  up -d`.
- 2026-10-03 (group 50)  #13 item: `api-gateway/batch_worker.py::run_in_batches` dropped a whole batch from the
  totals when `asyncio.gather` itself failed. The worker gather uses `return_exceptions=True`, so a worker error
  never reaches the `except`; what does is a failure of gather itself. That branch set `raw = []` after cancelling
  the tasks, and `zip(to_fetch, raw)` then skipped every item of the batch: not counted in `processed`, not in
  `results`, not in `errors`. The scan's universe total silently came up short, the batch's progress callback
  reported the old `processed` figure, and `main.py`'s scan summary (which reads `processed`/`errors`) never
  mentioned those symbols. The branch now rebuilds one entry per item after the cancel: a task that had already
  finished keeps its real result or its own exception, and one that was cancelled (or would not stop within
  `_cancel_tasks`' 2 s) is recorded as failed with the gather error (`{"item": ..., "error": "RuntimeError:
  boom"}`), through the same per-item path as any other failure, so `collect_errors_from_exceptions=False` still
  suppresses the error entry but the item is still counted. Invariant now tested: `processed == len(results) +
  len(errors)` for a batch whose gather failed. Tests (`tests/test_batch_worker.py`, 75 total): the old
  `test_gather_failure_cancels_tasks_and_moves_on` pinned the bug (`processed == 1`, item "neither processed nor
  recorded") and now asserts `processed == 2` and the recorded error; 5 new tests cover a full 5-item batch, the
  progress callback, a mix of finished / failed / cancelled workers, error collection off, and a failed batch not
  touching the next one. 6 of the 75 fail on the group 49 code; module coverage stays 100% / 100% (130
  statements, 54 branches); 3 repeated runs green. Sandbox: `tests/test_batch_worker.py` run directly. Not
  changed: a gather failure is only reachable in practice by an outer cancellation / interpreter-level fault, so
  this is a correctness-of-accounting fix, not a fix for a failure seen in production. NEEDS REBUILD:
  `docker compose build api-gateway && docker compose up -d`.
- 2026-10-03 (group 49)  #13 item: `api-gateway/instant_scanner.py` crashed on a `None` / non-dict feed. First
  recorded in the session 141 notes as "`_extract_price` fallback crashes on `feed=None` (`feed.get`)". Re-checked
  against the code: the same unguarded `.get()` was in three more places. `_extract_price`'s `except` branch looped
  over `(tick or {}, feed)` and called `src.get(...)` on the feed (and on a non-dict tick); `compute_technical_score`
  and `compute_fundamental_score` called `feed.get("technical_score")` / `feed.get("fundamental_score")` first
  thing; `_metrics` called `feed.get("metrics")`. All real callers pass a dict (`compute_instant_scores` and
  `process_single_stock` normalise first), so nothing failed in production, but the fallback only runs when
  `price_resolver` is already failing, so a bad row there would have turned "no data" into an exception inside a
  scan worker. New `_as_dict()` returns the value if it is a dict and `{}` otherwise; it is applied in `_metrics`,
  in `_extract_price`'s fallback (feed AND tick), and at the top of both score functions. A bad feed no longer
  hides a good tick, and a bad tick no longer hides a good feed. Scores for a bad feed equal the empty-feed scores
  (fundamental default baseline 84). Tests (`tests/test_instant_scanner.py::TestNonDictInputsDoNotCrash`, 67
  new, 416 total): None / str / list / int / 0 / tuple through `_as_dict`, `_metrics`, `_extract_price` (broken
  resolver and missing resolver), both score functions, `compute_instant_scores` and `process_single_stock`; 43 of
  them fail on the group 48 code. Branch coverage of the module stays 100% / 100% (253 statements, 124 branches).
  Left alone on purpose, still open in #13: the price-only PREPARE TO BUY case (a priced row with no feed data is
  flagged `provisional_defaults` but can still reach a decision from default indicators; that changes what the board
  shows, so it is its own item). The session 141 note about it and about `from_data_feed` possibly being a dict/str
  are unchanged. Sandbox: `tests/test_instant_scanner.py` run directly, 416 passed. NEEDS REBUILD:
  `docker compose build api-gateway && docker compose up -d`.
- 2026-10-03 (group 48)  First #13 item: `api-gateway/nse_holidays.py::is_nse_holiday()` answered False for a
  `datetime`. The holiday set holds plain `date` objects and a `datetime` neither equals nor hashes like a `date`, so
  `is_nse_holiday(datetime(2026, 9, 14, 10, 0))` (Ganesh Chaturthi, a closed day) returned False with no error; the
  old test even pinned that as a "documented gotcha". Every current caller passes `.date()` (`main.py`,
  `surprise_scanner.py`, `data_feed.py`), so nothing was wrong in production today, but one forgotten `.date()` would
  have scanned/notified on a holiday. New helper `_as_calendar_date()` reduces a datetime to its IST calendar date
  before the lookup, used by both `is_nse_holiday()` and `holiday_name()`: a naive datetime is read as IST wall-clock
  time (all callers build it from an IST `now`), an aware one is converted to IST first (2026-09-13 20:00 UTC is the
  14th in India), a `date` is unchanged, and `None` / strings / ints are still "not a holiday" rather than raising.
  `real-trade-service` and `position-stocks-service` have a different `is_nse_holiday(now: datetime)` that already
  converts to IST and is untouched. Tests (`tests/test_nse_holidays.py`): the old "datetime is not a match" test is
  replaced by naive-holiday, naive-ordinary-day, midnight boundaries, aware-IST, aware-UTC across the date line, `date`
  unchanged, non-date values, and `holiday_name` for a datetime; 25 pass, and 5 of them fail on the group 47 code.
  `scripts/check_holiday_lists_sync.py` still reports all 4 lists agreeing on 16 dates. Sandbox: pytest only,
  `tests/test_nse_holidays.py` and the kv drift guard run (84 passed); the gateway's other tests that need httpx etc.
  were not run, so run the gateway suite on your VM. NEEDS REBUILD: `docker compose build api-gateway && docker compose up -d`.
- 2026-10-03 (group 47)  Item #12, the six `kv_cache.py` copies: compared, nothing merged, nothing broken. There
  are three variants, not "identical copies": four byte-identical plain copies (analysis-intelligence
  `fundamental/`, decision-prediction `decision/` and `training/`, notification-scheduler `notification/`);
  `market-data-service` = plain + the `fundamentals:` durable prefix (its own `/fundamentals/{symbol}` cache); and
  `api-gateway` = plain + durable prefixes `stockky:hot_premarket_job`, `stockky:ipo:`, `stockky:ipoalerts:`,
  `system:surprise_feed`, `system:bulk_quote_cache`, `stockky:hot_stocks`, `stockky:surprise_scan:` + the stale-read
  helpers `kv_get_stale()` / `get_stale()` (the only other difference is an unused `text` import dropped from
  `_get_neon`). The differences are intentional: every key the other services read or write through their copy
  (rate-limit stats/events, notification config, `stockky:decide_cache:`, `indianapi:`) is already durable there, the
  90 s `trades:` report cache is deliberately memory-only, and no file outside `api-gateway` references a
  gateway-only prefix or helper (likewise nothing in the gateway uses `fundamentals:`). Merging would only make
  every service carry prefixes it never uses. The risk is silent drift: a prefix missing from a copy does not raise,
  `_is_durable()` just returns False and the key becomes memory-only until the next restart. New
  `api-gateway/tests/test_kv_cache_drift.py` (59 tests, reads the copies as source, no imports) pins: all six copies
  found and no seventh; the four plain copies byte-identical; no copy drops a shared prefix; market-data extras are
  exactly `{fundamentals:}` and gateway extras exactly the seven above; keys several services write are durable in
  every copy; the stale helpers exist only in the gateway copy and nobody else calls them; no other service uses a
  gateway-only prefix. Checked against four deliberate breakages (dropped prefix, foreign use of `stockky:ipo:`, a
  one-line drift in a plain copy, gateway using `fundamentals:`): each fails the guard, and the clean tree passes.
  No source changes. Sandbox had pytest only for this file (59 passed); run `pytest tests/test_kv_cache_drift.py`
  (or the gateway's full run) on your VM. No rebuild needed.
- 2026-10-03 (group 46)  Removed three stale `coverage annotate` artefacts from `real-trade-service`:
  `exit_engine/exit.py,cover`, `portfolio/portfolio.py,cover` and `execution/dhan_client.py,cover` (~230 KB; each is a
  line-by-line copy of its source with `>` / `!` coverage marks, and two of them no longer matched their source:
  exit.py 1622 lines vs 1611, dhan_client.py 1257 vs 1244). Nothing in the repo references them (checked every file
  outside the changelog and archive notes). Added `*,cover` to `.gitignore` so a future `coverage annotate` run does
  not put them back into the tree. Not done: there is no `.dockerignore`, so `COPY . .` in the service Dockerfiles
  still copies whatever is in the directory at build time; add one only if you want to keep annotate output and other
  local files out of the images (it changes the build context, so check each service's needs first). No code or test
  changes. No rebuild needed.
- 2026-10-03 (group 45)  `purge_legacy_universe_adx` ran a DELETE + commit on every volume-shock candidate
  cycle although its comment (and the call-site comment) called it a one-off. `real-trade-service/
  adaptive_market_params.py` now keeps a per-process flag (`_legacy_adx_purged`): the first purge that completes
  (including one that deletes nothing) sets it and later calls return 0 without touching the DB;
  `purge_legacy_universe_adx(db, force=True)` runs anyway. A failed purge (query or commit error, already
  swallowed and rolled back) leaves the flag unset, so it is retried next cycle. A restart purges once more, which
  also catches legacy rows written by an older instance during a rolling deploy. The call site in
  `candidate_engine/candidates.py` is unchanged apart from its comment. This entry also documents the ADX change
  that never had one (written from the code and its comments; the original date is not recorded there):
  `analysis-intelligence-service/technical/main.py` ADX switched from plain rolling means to Wilder smoothing
  (`_wilder_smooth`) and stopped reporting a fake 15 / NaN-turned-0.0 for histories too short for two Wilder
  periods. The old readings were a different, systematically higher and polluted measure, so the regime metric was
  renamed `universe_adx` -> `universe_adx_wilder` (`UNIVERSE_ADX_METRIC`; old name `LEGACY_UNIVERSE_ADX_METRIC`):
  `adaptive_signal_weights` stays on static 1.0/1.0 weights until `ADAPTIVE_MIN_HISTORY_DAYS` of new-method
  readings exist. Tests (`tests/test_adaptive_market_params.py`, `TestPurgeLegacyUniverseAdx`): once-per-process,
  no-op counts as done, `force`, retry after a failed query, retry after a failed commit; the existing four purge
  tests now reset the flag per test. Sandbox had no pytest/sqlalchemy, so the SQLite-backed tests were NOT run:
  the purge logic was checked with 8 assertions against stub modules and a fake DB (all pass on the fix, the
  once-per-process one fails on the group 44 code). Run `pytest tests/test_adaptive_market_params.py
  tests/test_candidates_orchestration.py` (or the service's full run) on your VM.
  NEEDS REBUILD: `docker compose build real-trade-service && docker compose up -d`.
- 2026-10-03 (group 44)  Loose ends from group 33 (debt-to-equity scale). (1) NSE `secInfo` scale: resolved by
  reading the code, not by guessing a scale - `market-data-service/main.py::_fetch_nse_fundamentals` hard-codes
  `secInfo["debtToEquity"]` to `None` (NSE quote-equity carries no ratios), so the NSE fallback never produces a
  D/E value and its scale cannot matter; nothing was rescaled. Pinned by tests (even a `debtToEquity` present in
  the NSE payload is not read), with a note that a real source wired in there later must have its scale decided
  and normalised at that time. (2) Untested Yahoo call site: the D/E selection inside `_get_fundamentals_inner`
  moved, behaviour unchanged, into `_debt_to_equity_from_yahoo(info, balance=None)` (info key present -> Yahoo
  percent via `_normalize_de_ratio(..., yahoo_percent=True)`, and an unusable value is None with NO balance-sheet
  fallback; key absent -> Total Debt / Total Equity Gross Minority Interest as an unscaled multiple, None on zero
  equity or missing rows). `_get_fundamentals_inner` now calls it with `balance if balance_available else None`.
  New tests in `market-data-service/tests/test_main_helpers.py`: 20 for the helper (percent scaling, numeric
  string, financial-sector rule, 4 unusable values, balance fallback, zero/negative equity, missing rows) and
  end-to-end tests that run the real `_get_fundamentals_inner` against a fake Yahoo ticker (30 -> 0.3x, 95.4 ->
  0.95x, bank 150 stays, absent stays None) and through the NSE fallback (debt_to_equity None). The end-to-end
  and NSE tests also pass on the group 43 code (they pin existing behaviour); removing `yahoo_percent=True` makes
  the helper and call-site tests fail. (3) NOT fixable in code: models trained on rows stored with the old D/E
  scale (and 24h-cached values) still carry the old scale until retrained / expired. Retrain the
  decision-prediction-service models, or wait out the 24h cache; there is no scale marker on stored rows to filter
  on. Sandbox had no pytest/fastapi/httpx/pydantic: `test_main_helpers.py` ran under a pytest stand-in with stub
  modules, 159 passed and 2 failed, the same 2 (`TestCooldown::test_non_yf_name_only_in_upstream_dict`,
  `TestCacheHelpers::test_should_soft_refresh_true_when_ttl_low`) that also fail on the original file under that
  stand-in, so they are runner artefacts; run `bash run_tests.sh` in market-data-service for the real result.
  NEEDS REBUILD: `docker compose build market-data-service && docker compose up -d`.
- 2026-10-03 (group 43)  `wire_peer_multi_quarter.apply_to_analyze_response` was not idempotent. Each call
  blended `fundamental_score` (0.70 base + 0.20 peer + 0.10 consistency) and overwrote `fundamental_score_raw`
  with whatever `fundamental_score` held, so a second pass blended the already-adjusted score again (80 -> 71 ->
  64.7) and destroyed the real raw score. Latent today: `fundamental/main.py` calls it once per `analyze()`, but
  any retry, re-wrap or second caller would compound silently. Now, when the payload already has
  `fundamental_score_adjusted is True` and a numeric (non-bool, non-NaN) `fundamental_score_raw`, the blend starts
  from that raw score, so applying it N times equals applying it once. A stray raw field without the adjusted
  flag, or an unusable raw value (string, None, bool, NaN), is ignored and the current score is blended as before.
  Tests (`analysis-intelligence-service/tests/test_wire_peer_multi_quarter.py`): the pin
  `test_applying_twice_compounds_and_overwrites_the_raw_score` rewritten as `test_applying_twice_is_idempotent`,
  plus 3-pass stability, a second pass through enrichment, flag-without-raw, 4 unusable-raw shapes and
  raw-without-flag; 3 fail on the group 42 code. Not changed: if enrichment inputs differ between passes the
  result follows the new inputs (only the score compounding is removed). Sandbox had no pytest/httpx: this file
  (88 tests) was run under a small pytest stand-in with a stub httpx, all pass; the full
  analysis-intelligence suite was NOT run - run `bash run_tests.sh`.
  NEEDS REBUILD: `docker compose build analysis-intelligence-service && docker compose up -d`.
- 2026-10-03 (group 42)  `surprise_premarket.bulk_baselines_from_yfinance` skipped its inter-batch pause after a
  failed or empty batch. The `time.sleep(YF_BULK_BATCH_PAUSE)` sat at the bottom of the loop, after the
  `continue`s for a raised `yf.download()` and for a None/empty frame, so exactly the batches that most likely hit
  a 429 got no pause before the next call. The pause now sits at the top of the loop (`if i > 0`), so every batch
  after the first is preceded by one whatever happened to the previous batch; successful runs sleep the same
  number of times as before (batches - 1) and a single batch never sleeps. Fixed in both copies
  (`api-gateway/surprise_premarket.py` and `market-data-service/surprise_premarket.py`; the two files differ
  elsewhere, only this loop was changed). api-gateway `tests/test_surprise_premarket.py`:
  `test_no_pause_after_a_failed_or_empty_batch` (pinned `sleeps == []`) rewritten as
  `test_pause_before_every_batch_after_the_first_even_when_earlier_ones_fail`, plus
  `test_pause_lands_before_the_next_download_not_after_the_last` and `test_single_batch_never_pauses`.
  market-data-service had no direct tests of this function, so new `tests/test_surprise_premarket_bulk_pause.py`
  (6 tests). Sandbox had no pytest/sqlalchemy: the bulk-yfinance tests were run under a small pytest stand-in
  (api-gateway: 2 new tests pass on the fix and fail on the group 41 code; market-data: 6 pass, 3 of them fail on
  the group 41 code). The full api-gateway and market-data suites were NOT run - run `bash run_tests.sh` in both.
  NEEDS REBUILD: `docker compose build api-gateway market-data-service && docker compose up -d`.
- 2026-10-03 (group 41)  yfinance env vars parsed at import without a guard, so one typo stopped the service.
  `YFINANCE_HARD_TIMEOUT_SEC` (`float(...)`) and `YFINANCE_POOL_WORKERS` (`int(...)` into the
  ThreadPoolExecutor) were read at module import in `rate_limiter.py`; "18s", "many", or an unusable value
  (0 workers, a zero/negative/NaN/inf timeout) raised out of the import. The pasted list named only
  analysis-intelligence-service `fundamental/rate_limiter.py`, but notification-scheduler-service
  `scheduler/rate_limiter.py` had the identical lines; both fixed (the api-gateway and market-data copies do not
  read these vars). New `_env_number(name, default, cast, valid)`: blank/unset -> default silently; invalid ->
  WARNING `rate_limiter: ignoring invalid <NAME>=<raw> - using default <d>` and the default (18s / 8 workers).
  Each var is parsed independently, so one bad value does not discard the other. Strict parsing: a fractional
  worker count ("3.5") or "1e2" workers is treated as invalid, not rounded. Old pins
  `test_bad_timeout_env_breaks_import`, `test_bad_worker_count_env_breaks_import` and
  `test_zero_workers_breaks_import` (analysis-intelligence `test_rate_limiter.py`, asserted ValueError) rewritten
  as fallback+warning cases (+ independence, blank, whitespace/fraction accepted, default timeout still enforced).
  New `notification-scheduler-service/tests/test_rate_limiter_env.py` (that service had no rate_limiter tests):
  39 tests in the service now pass, 16 of the new ones fail on the group 40 code. Sandbox: analysis-intelligence
  `run_tests.sh` all files pass (2012 tests), 100% on all modules; notification-scheduler tests 39 passed.
  NEEDS REBUILD: `docker compose build analysis-intelligence-service notification-scheduler-service && docker compose up -d`.
- 2026-10-03 (group 40)  api-gateway `symbol_aliases.resolve_with_fallback` (two bugs). (1) The learned-rename
  branch did `learned[base].get("to")` unguarded, so a malformed durable entry (a bare string, list, number or
  None left in the KV store) raised AttributeError out of the failure-recovery path; `_apply_all_renames` already
  guarded this with isinstance. (2) It returned only the FIRST rename hop: `mindtree` came back as `LTIM.NS`
  while `resolve_ns_ticker` / `_apply_all_renames` gave `LTM` (MINDTREE -> LTIM -> LTM), and a learned rename was
  never chased through the static chain. New helpers `_learned_target` (only `{"to": "<non-empty str>"}` counts,
  stripped; anything else is ignored) and `_chase_static_renames` (the existing cycle-guarded loop, extracted);
  `_apply_all_renames` and `resolve_with_fallback` both use them, so the three resolvers now agree. A malformed
  learned entry now falls through to discovery / unresolved instead of raising. Side effect: a learned entry whose
  "to" is a non-string (e.g. 5) is now ignored in `_apply_all_renames` too (it used to be accepted). `info["to"]`
  is now the final ticker; for one-hop renames (ZOMATO -> ETERNAL, LTIM -> LTM) nothing changes. Tests: 13 new
  cases (multi-hop static, learned->static chain, cycle, 9 malformed-entry shapes, strip) plus 3 for
  `_apply_all_renames`; 13 fail on the group 39 code. Sandbox: api-gateway `run_tests.sh` all files pass (7384
  tests), 100% on all modules. NEEDS REBUILD: `docker compose build api-gateway && docker compose up -d`.
- 2026-10-03 (group 39)  api-gateway `symbol_aliases.KNOWN_DELISTED` lacked AAKASH and ANNAPURNA, which
  market-data-service's `KNOWN_DELISTED_SYMBOLS` already short-circuits (confirmed dead by Yahoo 404 logs,
  2026-09-01). The gateway's resolvers (`resolve_ns_ticker`, `resolve_base_symbol`, `resolve_with_fallback`,
  `is_known_delisted`) therefore still mapped both to `<SYM>.NS` and sent them to yfinance, and kept them
  in the scan universe. Both are now in the gateway dict with a reason string (the dict value is what
  `resolve_with_fallback` returns as `detail`). New drift guard `tests/test_known_delisted_drift.py` reads
  market-data's set as source (no import) and requires the two lists to be identical, so a symbol added to
  one service can't be forgotten in the other again. Tests: AAKASH/ANNAPURNA added to the
  `is_known_delisted`, `resolve_ns_ticker`/`resolve_base_symbol` and `resolve_with_fallback` cases plus a
  "delisted, not renamed" pin; 18 of the new/extended cases fail on the group 38 code. Sandbox: api-gateway
  `run_tests.sh` all files pass (7365 tests), 100% on all modules; market-data `test_main_helpers` 139 passed.
  Only api-gateway code changed. NEEDS REBUILD: `docker compose build api-gateway && docker compose up -d`.
  Caveat: the symbols are treated as dead on the strength of the 2026-09-01 Yahoo 404s; if either ever
  relists, remove it from BOTH lists (the drift guard will fail until you do).
- 2026-10-03 (group 38)  api-gateway `hotpicks_store.hotpicks_repair_scores` never set `attempted`: only the
  price pass (`hotpicks_repair_batch`) did, so a score repair that tried rows reported `attempted: 0`, and the
  combined `POST /stockky-hot/repair-batch` total (price + score) under-counted. The score pass now sets
  `attempted = len(targets)` after the limit clamp, the same meaning as the price pass (rows tried, whether or
  not they ended up repaired; 0 when nothing needs scores). The old pin in
  `test_copies_decision_scores_and_levels` asserted `"attempted": 0` next to a successful repair and is
  rewritten to 1; 5 new tests (every row tried counts incl. non-200/empty/exception, 0 when nothing needs
  scores, limit clamp 15/3/100, forced symbol). 6 tests fail on the group 37 code. Sandbox: full api-gateway
  `run_tests.sh` all files pass, 100% on all modules. NEEDS REBUILD: `docker compose build api-gateway && docker compose up -d`.
  Note: items #1 (group 36) and #2 (group 37) of the pasted open-bug list were already fixed in the group 37 zip.
- 2026-10-03 (group 37)  Neon URL normaliser, doubled `&&` (14 copies): stripping a `channel_binding=`
  param from the MIDDLE of the query ("?a=1&channel_binding=require&b=2") left "a=1&&b=2", which libpq
  rejects (empty key). real-trade-service (session97) and position-stocks-service (session112) were
  fixed earlier; the rest were not. Now every copy collapses `&{2,}` before the leading/trailing
  cleanup: api-gateway (`hotpicks_schema`, `ipo_schema`, `surprise_schema`, `surprise_premarket`,
  `surprise_scanner`, `kv_cache`), market-data-service (`surprise_premarket`, `kv_cache`),
  analysis-intelligence-service `fundamental/kv_cache`, decision-prediction-service (`training/kv_cache`,
  `decision/kv_cache`, `training/universe_ingest`, `training/models`), notification-scheduler-service
  `notification/kv_cache`. Production was unaffected while Neon puts `channel_binding` last. Old pin
  `test_channel_binding_middle` (api-gateway and analysis-intelligence `test_kv_cache`) asserted the
  buggy "a=1&&sslmode=require" and is rewritten; middle-position cases added to the api-gateway
  hotpicks/ipo/surprise schema, surprise_premarket and surprise_scanner tests and to market-data's
  kv_cache and surprise_premarket tests. New drift guard `api-gateway/tests/test_db_url_normalizer_drift.py`
  reads every service's copies as source (no imports) and checks each one collapses `&&`; it is 32 tests
  and 26 of them fail on the group 36 code. Sandbox: api-gateway 1176 passed (the six touched modules
  plus the guard), analysis-intelligence kv_cache 240, market-data 176. The decision-prediction and
  notification copies are covered by the guard only (their modules were not imported here). NEEDS REBUILD:
  api-gateway, market-data-service, analysis-intelligence-service, decision-prediction-service,
  notification-scheduler-service.
- 2026-10-03 (group 36)  `oracle_compat.exec_ddl_safe` (all 8 byte-identical copies, kept identical): a real
  DDL failure (anything other than "already exists" / the benign ORA codes) used to be logged at DEBUG
  only, and callers then logged "ensured index ..." anyway, so a failed index/column was invisible.
  Now it returns a bool (True = ran or already there, False = genuinely failed), logs
  `exec_ddl_safe FAILED (<dialect>): <error> [sql: <first 100 chars>]` at WARNING, and still never
  raises (startup must not die on one index). SQLite's "duplicate column name" is also treated as
  benign. The three "ensured index" callers (real-trade-service `_ensure_hot_path_indexes` and
  `_ensure_nextday_watchlist_indexes`, position-stocks-service `_ensure_hot_path_indexes`) log
  "could NOT ensure index ..." at WARNING on False; a stub returning None still counts as success, so
  existing monkeypatched tests are unaffected. The other callers (kv_cache, hotpicks/ipo/surprise
  schema, angelone_ws_feed) ignore the result and now get the WARNING for free. Old pin
  `test_a_real_ddl_error_is_swallowed_by_exec_ddl_safe_but_still_reported_as_ensured` rewritten
  (both ensurers); DEBUG-level expectations in the api-gateway / analysis-intelligence / position-stocks
  oracle_compat tests changed to WARNING; new tests for the return value, SQL-in-log, duplicate-column
  and the three callers. Sandbox: oracle_compat tests api-gateway 108, analysis-intelligence 108,
  market-data 39, position-stocks 110 (+2 skipped) passed; real-trade 4 pre-existing failures (oracledb
  not installed, identical on the group 35 upload); test_db real-trade 225 / position-stocks 49;
  api-gateway kv_cache + hotpicks/ipo/surprise schema 548; analysis-intelligence kv_cache 239. The new
  tests fail against the old oracle_compat. NEEDS REBUILD: all services that bundle oracle_compat
  (`docker compose build` then `up -d`); expect WARNING lines only if a DDL genuinely fails.
- 2026-10-03 (group 35) — `notifier.py` in real-trade-service and position-stocks-service: a FAILED
  delivery no longer eats the 5-minute dedup window. `_should_send` still reserves the slot before
  delivery (keeps simultaneous identical sends race-free), but when every channel fails the slot is
  shortened to `_DEDUP_RETRY_AFTER_FAIL_S` (30s), so the same alert (SELL failed / exit blocked) is
  retried after 30s instead of being reported "sent" and never attempted for 5 min. A call
  suppressed during that back-off now returns False (was True). A hard outage still cannot storm:
  one real attempt per 30s per message. Covers notify_sync, notify_async, notify_fire_and_forget
  (incl. a crash inside the background thread). Old pin `test_a_failed_delivery_still_consumes_the_dedup_slot`
  rewritten + 9 new tests (real-trade) and 6 (position-stocks). notifier.py 100% in both.
  real-trade test_notifier 81 passed; position-stocks full suite 2339 passed. Unrelated sandbox
  failures (oracledb/dhanhq not installed) fail identically on the original upload. NEEDS REBUILD:
  `docker compose build real-trade-service position-stocks-service && docker compose up -d` both.
- 2026-10-03 (group 34) — api-gateway `requirements.txt`: added `pyjwt==2.8.0` (same pin as
  real-trade-service / position-stocks-service). It was only in `requirements-test.txt`, so the
  Docker image had no PyJWT and `qstash_client.verify_signature` hit its ImportError path and
  accepted every `/ops/qstash/tick` callback unverified (warning log only). New drift guard
  `tests/test_requirements_pins.py` (3 tests: PyJWT in production requirements, exact pin, matches
  the other services). `requirements-test.txt` comment updated. The fail-open-on-ImportError
  behaviour in `qstash_client.py` is unchanged. NEEDS REBUILD: `docker compose build api-gateway`
  then restart; confirm with `docker compose exec api-gateway python -c "import jwt;print(jwt.__version__)"`.
  Tests: test_requirements_pins + test_qstash_client 31 passed (sandbox, httpx/pytest only; full
  `bash run_tests.sh` not run here, re-run on the VM).
- 2026-10-02 (group 33) — debt-to-equity scale fixed at the source. `market-data-service/main.py`
  `_normalize_de_ratio` gained `yahoo_percent=False`; the Yahoo `info["debtToEquity"]` call site
  now passes `True`, because Yahoo's field is ALWAYS a percent (30 = 0.3x), so non-financials are
  divided by 100 at any magnitude (before, a Yahoo "30" stayed 30 and was scored as 30x leverage
  and penalised). Financial-sector rule and the default (flag off) heuristic unchanged; the NSE
  `secInfo` fallback is untouched (its scale is unknown). The `analysis-intelligence-service` pin
  in `test_fundamental_main.py` is rewritten as an input contract (values <=50 are taken as
  already-normalised multiples; rescaling there would double-divide). IMPACT: stocks with Yahoo
  D/E under ~50% will now score as low leverage, the fundamental score rises for them, and
  `decision-prediction-service` features built from stored `debt_to_equity` change scale for new
  rows (models trained on old rows, and 24h-cached values, see the old scale until retrained /
  expired). The one-line call site is not covered by any test (no existing test exercises
  `_get_fundamentals_inner`); the helper has new unit tests. market-data suite 577 passed.
- 2026-10-02 (group 32) — removed the remaining hardcoded "Aug-2026: Nifty -7% 6m, FII net-short"
  claims that never tracked real data: `real-trade-service/candidate_engine/candidates.py`
  (`market_note` is now `""`), `api-gateway/surprise_scanner.py` (`market_note` is now
  "high buy_pct = strong signal" on hits and "thresholds raised for quality" on the scan summary),
  `decision-prediction-service/decision/main.py` (regime label for market_score < 38 is now
  "Correction (weak market regime)"). Keys/shapes unchanged; the frontend does not read
  `market_note`. Tests updated in `test_surprise_scanner.py` and `test_candidates_analysis.py`.
  Comments/docstrings mentioning Aug-2026 left alone. decision-prediction-service has no test
  suite (syntax-checked only). api-gateway `run_tests.sh` exit 0 / 100%; real-trade-service
  `pytest --cov=.` 2751 passed, 1 skipped, 100%.
- 2026-10-02 (group 31) — api-gateway `main.py` `/ops/check-alert`: added a per-process cool-down.
  The same problem set (same open-circuit set / error-rate condition) is notified at most once per
  `OPS_ALERT_COOLDOWN_SEC` (default 900; 0 disables; bad value -> 900). A new problem alerts
  immediately, recovery clears the state, and the cool-down is only recorded after a SUCCESSFUL
  delivery (a failed send retries next call). Suppressed calls return `alerted: false,
  suppressed: true, cooldown_remaining_sec`. State is in-memory, so a restart can repeat one alert.
  Pin in `test_main_ops_routes.py` rewritten + 7 new tests. `bash run_tests.sh` for api-gateway:
  exit 0, 100% coverage.
- 2026-10-02 (group 30) — api-gateway `ipo_scanner.py`: dropped the hardcoded "Market context
  (Aug-2026): Nifty -7% in 6m, FII net-short" sentence from every IPO `buy_suggestion.rationale`
  (it never tracked real data and was already stale); the rationale now ends with the bars in
  force: "Decision bars: BUY_NOW≥70, PREPARE≥58." Removed the matching stale comment in
  `_build_ipo_suggestion`. Pin in `test_ipo_scanner.py` rewritten. NOT changed: the same stale
  Aug-2026 market text still appears in `real-trade-service/candidate_engine/candidates.py`
  (`market_note`), `api-gateway/surprise_scanner.py` (`market_note`, 2 places),
  `decision-prediction-service/decision/main.py` (a label) and in docstrings. `bash run_tests.sh`
  for api-gateway: exit 0, 100% coverage.
- 2026-10-02 (group 29) — api-gateway `main.py` `/ops/qstash/tick`: removed the no-op "warm" list
  (it only appended the names `/health` and `/ops/keepalive`, nothing was called) and the unused
  `_get_http_client()` call; the response is now `{"ok": true, "source": "qstash"}` plus
  `keepalive` / `keepalive_error` when the body asks for a wake. Nothing in the repo reads
  `warm`. Pin in `test_main_ops_routes.py` rewritten. The alert cool-down pin is left as is (needs
  a de-dup state/TTL design). `bash run_tests.sh` for api-gateway: exit 0, 100% coverage.
- 2026-10-02 (group 28) — api-gateway `main.py` `/ops/qstash/tick`: a crash inside the QStash
  signature check now fails CLOSED (503 "QStash signature verification unavailable", logged with
  traceback, nothing runs) instead of being swallowed and running the tick unauthenticated. Missing
  signing keys / PyJWT in `qstash_client.verify_signature` still accept by design (unchanged).
  Pin in `test_main_ops_routes.py` rewritten; log test added. Two other NOT FIXED pins in that file
  (alert cool-down, "warm" list) left as is, they need a product decision. `bash run_tests.sh` for
  api-gateway: exit 0, 100% coverage.
- 2026-10-02 (group 27) — real-trade-service, tests only, no production code changed. Fixed the 26
  failing tests in the full suite: (1) `test_feed_remaining_coverage.py` `_run` used
  `asyncio.get_event_loop()`, which raises on Python 3.12+ after any earlier file calls
  `asyncio.run()`; it now uses its own loop per call (19 tests). (2) `test_afterhours_scan_orchestration.py`
  hardcoded `pubDate="2026-09-24"` while `run_afterhours_scan` drops news older than
  `AFTERHOURS_SCAN_MAX_NEWS_AGE_DAYS` (5) against the real clock; the default is now yesterday
  (7 tests; these failed even when the file was run alone). `pytest --cov=.` in a clean venv:
  2751 passed, 1 skipped, 100% coverage.
- 2026-10-02 (group 26) — real-trade-service `auth/dhan_credentials.py`: `refresh_if_totp_enabled`
  now also rejects a missing/empty `DHAN_PIN` before any HTTP call (was sent as `pin=""`, wasting a
  TOTP attempt on a request Dhan can only reject). Pin in `test_dhan_credentials.py` rewritten
  (PIN added to the missing-credential parametrize, plus an empty-PIN test; log message now names
  all three vars). `test_dhan_credentials.py`: 157 passed. (Group 26 note corrected in group 27:
  the 26 full-suite failures were 19 in `test_feed_remaining_coverage.py` + 7 in
  `test_afterhours_scan_orchestration.py`, not all in the feed file; both fixed in group 27.)
- 2026-10-02 (group 25) — analysis-intelligence-service `fundamental/peers.py`: `normalize_sector`
  is now idempotent (canonical names like "IT", "Finance", "Infra", "Capital Goods" map to
  themselves). `analyze()` feeds the normalised sector back into `peers_for()`, which returned no
  peers for those four sectors for any symbol outside the curated list. Pin in
  `test_fundamental_main.py` rewritten to assert peers are returned; idempotency tests added in
  `test_peers.py`. The other fundamental pin (debt-to-equity <= 50 not rescaled) left as is: the
  percent-vs-multiple heuristic is ambiguous, needs your call. `./run_tests.sh` in a clean venv:
  exit 0, 100% coverage.
- 2026-10-02 (group 24) — analysis-intelligence-service `news/main.py`: `_base_symbol` now
  upper-cases/strips before removing the `.NS`/`.BO` suffix (and only strips a trailing one), so
  `tcs.ns` resolves to `TCS` and finds its company-name hint instead of searching news for
  `TCS.NS`. Pin in `test_news_main.py` rewritten to assert the fix. `./run_tests.sh` in a clean
  venv: exit 0, 100% coverage.
- 2026-10-02 (group 23) — analysis-intelligence-service `fundamental/rate_limiter.py`:
  `_cfg` now parses `RL_<PROVIDER>_RPS` and `RL_<PROVIDER>_BURST` independently, so a typo in
  one no longer discards a valid value for the other (pin in `test_rate_limiter.py` rewritten to
  assert the fix). Also fixed `test_event_depth.py::test_earnings_days_out_propagated`, which
  built its date from a hardcoded `_NOW` while the code reads the real clock (failed on the
  group 22 zip too). `./run_tests.sh` in a clean venv: exit 0, 100% coverage.
- `SESSION126_ANALYSIS_INTEL_INDIANAPI_FALLBACK_COVERAGE_2026-09-29.md` —
  analysis-intelligence-service `fundamental/indianapi_fallback.py` 93% -> expected 100%: kv_cache
  import fallback, in-process pacing sleep, suggested_timeout failure, redis-client guard. Tests only.
  35/35 passed via a hand-rolled pytest stand-in with line tracing (all 8 target lines hit); NOT run
  under real pytest/coverage — re-run on the VM. Next: `event/event_depth.py` (97%).
- `SESSION125_ANALYSIS_INTEL_MAIN_SENTIMENT_COVERAGE_2026-09-29.md` —
  analysis-intelligence-service: root `main.py` (real source executed against a temp fake-sub-app
  tree so failure branches count for the real file) and `sentiment/main.py` (batch/individual
  fallbacks, adjustment failures, double-checked cache, `__main__` block). Tests only. Passed in
  an earlier sandbox (1741 passed, both files 100%); re-applied here and compile-checked only —
  re-run on the VM. Next: `fundamental/indianapi_fallback.py` (93%).
- `SESSION124_ANALYSIS_INTEL_RATE_LIMIT_REPORT_COVERAGE_2026-09-29.md` —
  analysis-intelligence-service `rate_limit_report.py`: 35 test functions appended to
  `tests/test_rate_limit_report.py` covering the `_kv_get`/`_kv_set` sys.path fallback,
  stats/event edge cases, gateway POST and status extraction. Tests only; not run
  in-sandbox (no pytest) — confirm with the coverage command in the note.
- `SESSION123_ANALYSIS_INTEL_SERVICE_MAIN_COVERAGE_2026-09-29.md` —
  analysis-intelligence-service root `main.py`: new `tests/test_service_main.py`
  (fake sub-app trees + one real-mount smoke test; temp trees under `__pycache__` so
  they stay out of coverage). Fixed a wrong `''`-on-sys.path assertion. Tests only; not
  re-run in-sandbox (no pytest) — run `./run_tests.sh` to confirm. Session-122 roadmap
  is now complete.
- `SESSION112_ROUND29_CONFIG_ADMIN_HASH_B64_COVERAGE_2026-09-25.md` —
  position-stocks-service `config.py` 471-475 closed: the
  ADMIN_PASSWORD_HASH_B64 decode block, ported from real-trade-service's
  identical block + its `importlib.reload` fixture pattern. Pure stdlib —
  all 4 tests actually run for real in-sandbox (no stub needed), not just
  hand-traced. This clears the entire round-24 source-file list; only
  tests/*.py files' own coverage gaps remain, flagged round 28, still out
  of scope pending an explicit go-ahead.
- `SESSION112_ROUND28_REMAINING_ONE_LINE_GAPS_2026-09-25.md` — position-
  stocks-service: closed all five remaining 1-2 line source gaps from the
  round-24 list — `eod_squareoff.py` 147-148 (notify_critical exception
  swallow), `adaptive.py` 113 (ATR sample<2 guard, forced via monkeypatched
  `ATR_LOOKBACK`), `entry.py` 109 (reentry guard's unresolvable `closed_at`
  fail-open), `reconcile.py` 232 (legacy-backfill blank orderId skip),
  `screening/engine.py` 342 (non-positive composite score, forced via
  monkeypatched `_WINDOW_CONVICTION_MULT`). Tests only. `config.py`
  471-475 still open (importlib.reload risk); several `tests/*.py` files'
  own coverage gaps flagged but not yet in scope.
- `SESSION112_ROUND27_EOD_SQUAREOFF_STAGNATION_EXIT_COVERAGE_2026-09-25.md` —
  position-stocks-service `orders/eod_squareoff.py` 1044-1045 closed:
  `run_stagnation_exit`'s `except Exception: continue` guarding a
  malformed `opened_at`, via monkeypatched `as_aware` (same isolation
  convention as the class's other skip-and-continue tests). Tests only.
  Still open from round-24: `eod_squareoff.py`'s other 2 lines (needs a
  fresh coverage run to confirm), and the four 1-line gaps in
  `adaptive.py` / `entry.py` / `reconcile.py` / `screening/engine.py`.
- `SESSION112_ROUND26_WS_CLIENT_LOOP_COVERAGE_CLOSEOUT_2026-09-25.md` —
  position-stocks-service `feed/ws_client.py` last 4 missing lines closed
  (510-511, 542, 549): `_ws_loop`'s heartbeat-send exception guard, the
  stale-tick buffer-prune eviction, and the `_last_quote` write (needed a
  full ≥347-byte depth frame, ported from `test_ws_client.py`'s builder).
  Tests only. Next per the round-24 coverage run: `orders/
  eod_squareoff.py` (4), or the four 1-line gaps in `adaptive.py` /
  `entry.py` / `reconcile.py` / `screening/engine.py`.
- `SESSION112_ROUND25_DHAN_CLIENT_COVERAGE_CLOSEOUT_2026-09-25.md` —
  position-stocks-service `execution/dhan_client.py` all 10 remaining
  missing lines closed (156-157, 161-163, 244, 246, 252-253, 960-961):
  `_get_sdk_client`'s two SDK-version ImportError branches (forced via
  `sys.modules["dhanhq"]` patching), the CSV-fallback loop's exchange/
  instrument filters + per-row exception guard, and
  `edis_verification_summary`'s non-dict-row branch. Tests only. Next per
  the round-24 coverage run: `feed/ws_client.py` (4), `orders/
  eod_squareoff.py` (4), or the four 1-line gaps in `adaptive.py` /
  `entry.py` / `reconcile.py` / `screening/engine.py`.
- `SESSION112_ROUND24_DB_ORACLE_DDL_COVERAGE_2026-09-25.md` —
  position-stocks-service `db.py` `_ensure_columns`'s Oracle-dialect DDL
  branch (lines 296-298), tests only. New test in `tests/test_db.py`
  monkeypatches `DATABASE_URL` to an Oracle DSN to exercise the
  `is_oracle` ALTER-TABLE string. Picked off a real `pytest --cov` run
  (2257 passed) that confirmed round 23's fix and superseded the earlier
  unverified table. Next per that run: `execution/dhan_client.py` (10),
  `feed/ws_client.py` (4), `orders/eod_squareoff.py` (4), or the four
  1-line gaps in `orders/adaptive.py` / `entry.py` / `reconcile.py` /
  `screening/engine.py`.
- `SESSION112_ROUND23_CONFIG_GETTERS_COVERAGE_2026-09-25.md` —
  position-stocks-service `config.py` `_get_float`/`_get_int` malformed-env-var
  fallback branches (lines 31-32, 38-39), tests only. New
  `tests/test_config_getters.py` (8 tests). Picked up in a fresh
  conversation with no memory of the session that produced the prior
  coverage table — not re-verified against a real coverage run (no
  pytest/coverage in this sandbox, no network). See the note at the top
  of the session file. Next: re-run coverage for real before continuing
  down that table, since its line numbers may be stale.
- `SESSION112_ROUND16_DHAN_CLIENT_COVERAGE_2026-09-25.md` —
  position-stocks-service `execution/dhan_client.py` (the only module
  allowed to hold a decrypted Dhan credential / call Dhan's API) 21% → 
  target ~95%+, tests only, no production change. New
  `tests/test_dhan_client.py`: pure tick-rounding/classifier logic tested
  directly; every SDK-facing function (`place_order`, `place_super_order`
  incl. the MARKET direct-HTTP bump/clamp path, `get_trade_history`
  pagination, `place_cnc_stop_loss_market`'s 4 raise conditions, eDIS,
  `convert_position`, etc.) tested against a `SimpleNamespace` fake SDK
  client. No `sqlalchemy`/`httpx`/`dhanhq` in this sandbox — pure-logic
  math/classifiers re-verified standalone (all passed); SDK-facing tests
  traced by hand against the source, not executed — re-run pytest to
  confirm. No new production bug found this round. Next:
  `feed/scrip_master.py` (22%).
- `SESSION112_ROUND15_DB_TEST_FAILURES_FIX_2026-09-25.md` — first real
  pytest run of round 14's new `tests/test_db.py` (position-stocks-service)
  found 3 failures, all in `TestEnsureColumns`. **One real fix:**
  `_ensure_columns` built its `inspect(engine)` once, outside the
  per-entry `try/except`, so a failure there crashed `init_tables()`
  instead of being logged and skipped like every other migration failure
  — moved inside the loop. Two test-file bugs also fixed: a no-op test
  that only pre-created 1 of 12 columns, and a "failed ALTER" test whose
  `ctx.__enter__` override was an instance attribute the `with` statement
  never actually looked up (rewritten with `@contextlib.contextmanager`).
  `sqlalchemy`/`pytest` still unavailable in this sandbox (no network) —
  fixes are statically verified (`py_compile`) only; re-run pytest to
  confirm. Next: `execution/dhan_client.py` (21%).
- `SESSION112_ROUND7_SHARED_EXPOSURE_COVERAGE_AND_ROLLBACK_GUARD_2026-09-25.md`
  — position-stocks-service `capital/shared_exposure.py` 36% → 100% (whole
  `capital/` package now 100%). **One real fix:** `publish_own_exposure`'s
  except-handler called `db.rollback()` unguarded, so a dead connection made
  it raise despite its documented "never raises" contract, out of
  `ledger.sync_from_broker` and `POST /ledger/sync`; now guarded like its
  siblings (red test first, then fix). New `tests/test_shared_exposure.py`
  (36 tests) incl. unstubbed ledger→publish wiring and a cross-service drift
  guard. Suite 1577 → 1613 passed, 87%. 22 mutations, 0 survivors. Same
  unguarded rollback flagged (not changed) in real-trade-service's copy.
  Next: `tz_utils.py` (71%).
- `SESSION112_ROUND6_SHARED_ORDER_BUDGET_COVERAGE_2026-09-25.md` —
  position-stocks-service `capital/shared_order_budget.py` (cross-service Dhan
  order-rate guard) 55% → 100%, tests only. New
  `tests/test_shared_order_budget.py` (38 tests) incl. a stale-identity-map
  regression proving the cap is enforced by the atomic UPDATE, exits never
  gated, fail-open, `status()`. Suite 1539 → 1577 passed. 29 mutations, 0
  survivors. Flags `_get_or_create_row` as dead code (unchanged). Next:
  `capital/shared_exposure.py` (36%).
- `SESSION112_ROUND5_SHARED_SYMBOL_LOCK_COVERAGE_2026-09-25.md` —
  position-stocks-service `capital/shared_symbol_lock.py` (cross-service
  same-symbol guard) 30% → 100%, tests only. New
  `tests/test_shared_symbol_lock.py` (54 tests) incl. a REAL unique-constraint
  IntegrityError race (lost / won-by-self) and fail-open on every error path.
  Suite 1485 → 1539 passed, 84% → 86%. 36 mutations, 0 survivors. Flags one
  cosmetic `cleanup_stale` startup-log inaccuracy (not changed). Next:
  `capital/shared_order_budget.py` (55%).
- `SESSION112_ROUND4_LEDGER_COVERAGE_2026-09-25.md` — position-stocks-service
  `capital/ledger.py` (the money engine) 53% → 100%, tests only, no production
  change. New `tests/test_ledger_coverage.py` (82 tests) incl. the REAL
  `sync_peer_pnl` (shared fixture stubs it), capital-erosion add-back,
  kill-switch trip boundary + gate mirror, `EXIT_LEGS_REJECTED` handling.
  Suite 1403 → 1485 passed, 82% → 84%. 41 mutations, 0 real survivors (2
  provably equivalent). Next: `capital/shared_symbol_lock.py` (30%).
- `SESSION111_NOTIFY_FIRE_AND_FORGET_EXIT_PATH_FIX_2026-09-25.md` — `notify_sync`
  could block its caller for a ~42s worst case (service timeout + direct-Telegram
  timeout + its own HTML-retry timeout), and `exit_engine/exit.py` called it 14
  times inline while holding the per-mode exit lock — one slow Telegram delivery
  for one position's exit stalled protective stop-loss checks for every other
  open position in the same tick, and skipped the next 5-10s tick outright.
  Fixed with a new `notifier.notify_fire_and_forget` (same dedup + delivery,
  off a daemon thread, no return value) and a one-line import-alias change in
  `exit_engine/exit.py` — no call sites touched. real-trade-service 2725 passed,
  0 xfailed, 100% (8320 stmts). 30 mutations, 0 real survivors.
  `position-stocks-service`'s equivalent call sites not ported this round
  (flagged as open).
- `SESSION110_PARTIAL_FILL_INCREMENT_PRICING_AND_STAGE_TIMINGS_2026-09-24.md` —
  the last xfail is gone. **`execution/reconcile.py`** booked every partial-fill
  increment at Dhan's *cumulative* average price (5 @ 100 then 5 @ 102 booked as
  100 + 101: position average, cash and SELL P&L all drifted); each increment is
  now booked at its own price, derived from the previous poll's cumulative value
  (new nullable `trade_orders.broker_fill_notional`, additive migration), with
  fallbacks to the old behaviour whenever paise-rounding noise or inconsistent
  broker data makes the derivation untrustworthy; also wired into the
  `expire_stale_orders` late-fill path. **`pipeline_status`/`cycle_runner`:** stage
  timings for the three concurrent stages were misattributed since session48b —
  now exact. real-trade-service 2716 passed, 0 xfailed, 100% (8304 stmts).
  30 mutations, 0 real survivors.
- `SESSION109_REAL_TRADE_TAIL_COVERAGE_AND_FILLEDQTY_FIX_2026-09-24.md` —
  **real-trade-service production code 100%** (8226 stmts, 0 missed; 2667
  passed). Two silent-failure bugs fixed. (1) `execution/reconcile.py` stamped a
  `TRADED` order `FILLED` with no position/cash booked when Dhan's `filledQty`
  was non-numeric (never re-polled, so the fill vanished from the books) — now
  left pending and retried. (2) `risk_engine/engine.py`: a BUY with a **NaN**
  stop / entry / qty / adj_risk_pct was **APPROVED at full size** (NaN compares
  False, so every cap was skipped) — now rejected `invalid_order`; SELLs
  unaffected. The old "unreachable" sizing guard became the directly-tested
  `_qty_within_risk_cap()`. Also: never-awaited coroutine in
  `feed._schedule_atr_refresh`, legacy `Query.get()` in `exit._load_profile`, a
  vacuous test rewritten, `event_depth_local`/`return_sanity` 100% with a
  keyword-drift guard vs analysis-intelligence-service, new `.coveragerc`
  (omits dev harness + `tests/`, so not comparable with the old 98%).
  19 mutations, 0 survivors.
- `SESSION108_POSITION_STOCKS_ORACLE_COMPAT_COVERAGE_2026-09-24.md` —
  `position-stocks-service/oracle_compat.py` 15%→100% (110 stmts, 30/30
  branches; still 100% with the `# pragma: no cover` lines counted). Finishes a
  half-done draft (88%, 5 failing). New `tests/test_oracle_compat.py` (104
  tests): exact-string SQL for both dialects, the Postgres upsert executed for
  real on SQLite, real `exec_ddl_safe` DDL, the `connect` call-timeout listener
  fired from the pool's dispatch, lazy real Oracle engine build when `oracledb`
  is installed. position-stocks 1233→1337 passed, 78%→80%. 53 mutations, 0
  survivors. No production code changed. Next: `pipeline_status.py`,
  `capital/shared_symbol_lock.py`, `capital/shared_exposure.py`.
- `SESSION99_SECRET_IN_URL_LOG_LEAK_FIX_ROUND2_2026-09-24.md` — session98's
  "still open" secret-in-URL leak, closed in the two services it named, plus
  one more found along the way. **`market-data-service/main.py`:** TwelveData,
  Polygon and AlphaVantage API keys were in query strings, logged in full at
  INFO via `httpx` (same mechanism as session98). **`analysis-intelligence-
  service/news/main.py`:** same for the NewsAPI key (this service's first
  tests). Both fixed with an `httpx`-logger redaction filter, same shape as
  session98. **Also found:** `position-stocks-service/feed/ws_client.py` puts
  the AngelOne feed token and API key in the WS URL; the `websockets` library
  logs the full request line at DEBUG via its own `"websockets.client"`
  logger — lower severity (this service's `LOG_LEVEL` defaults to INFO, so it
  doesn't leak today) but fixed the same way since `LOG_LEVEL=DEBUG` is a real
  supported knob. All three reproduced live (real httpx `MockTransport` /
  real local `websockets` server-client round-trip) both leaking pre-fix and
  clean post-fix; regression tests fail on the old files. market-data-service
  24 passed (14+10 new); analysis-intelligence-service 8 passed (new);
  position-stocks-service 1232 passed (1225+7 new).
- `SESSION98_NOTIFIER_COVERAGE_AND_SECRET_IN_URL_LOG_LEAK_FIX_2026-09-24.md` —
  `notifier.py` 23%(unstable)→100%; new `tests/test_notifier.py` (64 tests, real
  httpx via MockTransport). **Security fix in 4 files / 3 services:** httpx logs
  every request URL at INFO and services run `basicConfig(INFO)`, so the
  Telegram **bot token** was in the logs on every send — and in
  `notification-scheduler-service` (the platform's *primary* alert path) also the
  **Discord/Slack webhook URLs** and **CallMeBot apikey**; a revoked webhook even
  returned its URL in the `/notify` response (`HTTPStatusError` embeds the URL).
  Fixed with an `httpx`-logger redaction filter (+ scrubbed error strings) in
  `real-trade-service/notifier.py`, `position-stocks-service/notifier.py`,
  `notification-scheduler-service/notification/main.py` and
  `scheduler/governance_check.py`; regression tests fail on the old code. **Check
  your logs and rotate secrets — see the note.** Same class still open in
  `market-data-service` / `analysis-intelligence-service` (API keys in query
  strings). real-trade-service 2253 passed/1 skipped/1 xfailed, 96%;
  position-stocks 1225 passed; notification-scheduler 21 passed (first tests
  there). 52 mutations, 0 survivors.
- `SESSION97_DB_MIGRATIONS_COVERAGE_AND_DRIFT_GUARD_2026-09-24.md` —
  `db.py` 7%→100% (521/521). New `tests/test_db.py` (217 tests): every one of
  the 22 boot-time migrations executed on a real *legacy* SQLite schema (Oracle
  branch captured via recorded SQL and checked for parity/type/length/default
  against `models.py`); legacy rows read back with the same defaults as new
  rows. **Drift guard:** adding a column to an existing model without an
  `_ensure_*` migration now fails a test (the session-11 bug class). Optional
  `tests/test_db_postgres_live.py` (5 tests, skipped without `pgserver`) runs
  the Postgres SQL on a real PostgreSQL. **One production fix:**
  `_normalize_pg_url` left `&&` when `channel_binding` sat mid-query, which
  libpq rejects. VM-equivalent run: 2189 passed, 1 skipped, 1 xfailed; 94%→96%.
  60 mutations, 0 survivors. Note: `notifier.py` coverage (52→48→23%) is
  incidental, not a regression — it has no direct tests; next candidate.
- `SESSION96_DHAN_CREDENTIALS_COVERAGE_AND_PIN_LEAK_FIX_2026-09-24.md` —
  `auth/dhan_credentials.py` 18%→100% (215/215). New
  `tests/test_dhan_credentials.py` (156 tests, real SQLite + real Fernet + real
  pyotp). **Two production fixes:** (1) `refresh_if_totp_enabled()` logged AND
  Telegrammed `str(HTTPStatusError)`, which contains the full request URL —
  `...generateAccessToken?dhanClientId=..&pin=<PIN>&totp=..` — so any 4xx/5xx
  leaked the Dhan PIN; now redacted via `_redact_secrets()` before log/notify
  (check your log/Telegram history — see note). (2) a swallowed DB failure in
  that function left the caller's Session in `PendingRollbackError` for the
  next query in `cycle_runner`; now healed via `_heal_session()` (rolls back
  only a poisoned session). Regression tests fail on the pre-fix code (24
  failures); mutation-checked (38 regressions, 0 survivors). Full suite: 1972
  passed, 1 xfailed; overall 93%→94%.
- `SESSION95_LOCAL_CACHE_COVERAGE_2026-09-24.md` — `resilience/local_cache.py`
  45%→100% (62/62). New `tests/test_local_cache.py` (32 tests) against a real
  in-memory SQLite DB: the 2026-09-12 two-writer `IntegrityError` race
  reproduced for real (loser's write lands via the UPDATE fallback), the
  2026-09-16 empty-positions snapshot regression, the 2026-09-12
  PARTIALLY_CLOSED reconcile fix, exact `RECONCILE_MISMATCH` audit detail,
  and a `String(64)` key-length audit of every key the service writes
  (longest 42 — no bug). Mutation-checked (14 regressions, 0 survivors).
  Full suite: 1816 passed, 1 xfailed; overall 93%. No production code
  changed. Observations: `json.dumps` sits outside `save_snapshot`'s `try`;
  startup reconcile is false-positive-prone by design (snapshot precedes
  exits).
- `SESSION94_CYCLE_RUNNER_COVERAGE_2026-09-24.md` — `cycle_runner.py`
  7%→100% (122/122). New `tests/test_cycle_runner.py` (64 tests) covers the
  function every REAL/DEMO cycle funnels through: manual market-hours
  warning, REAL token pre-flight (TOTP gating, early auto-disarm with nothing
  downstream executed), the session48b concurrent
  dynamic_universe→watchlist ‖ candidates stage (proven with events, not
  just call order), exit-lock acquire/release incl. a real `threading.Lock`,
  position snapshot, and the real `pipeline_status` contract.
  Mutation-checked (16 deliberate regressions, all caught). Full
  `real-trade-service` suite: 1784 passed, 1 xfailed; overall 92%→93%.
  No production code changed. **Finding, not fixed:** `pipeline_status`'s
  single "current stage" slot is overwritten by the three concurrent stages,
  so per-stage `stage_timings_ms` are misattributed since session48b
  (observability only) — see the note for numbers and options.
- `SESSION93_INTRADAY_ELIGIBILITY_COVERAGE_2026-09-24.md` —
  `intraday_eligibility.py` first direct coverage (every other test
  monkeypatched its public functions away, so its cross-service
  `scalp_intraday_restricted` mirroring had never run under test).
- `SESSION92_AFTERHOURS_SCAN_FINAL_COVERAGE_GAPS_2026-09-24.md` —
  `watchlist_engine/afterhours_scan.py` final 3 gaps closed (now 100%);
  VM run confirmed session91's rounds 1-3 (1694 passed, 1 xfailed).
- `SESSION91_AFTERHOURS_FETCHERS_COVERAGE_ROUND2_2026-09-24.md` — round 2:
  new `tests/test_afterhours_scan_fetchers.py` (20 tests) covers
  `_fetch_rss_items`, `_fetch_bulk_deal_hits`, and `_validate_symbols` —
  the three network/circuit-breaker-dependent functions in
  `watchlist_engine/afterhours_scan.py` — using the same
  `httpx.AsyncClient` + breaker-`.call()` mocking pattern already
  established in `tests/test_watchlist_sources.py`. **Not run through live
  pytest — no network in this sandbox**; hand-traced against the real
  source. Deferred to round 3: `run_afterhours_scan` and
  `finalize_nextday_watchlist`, the two DB-writing orchestrators. No
  production code changed.
- `SESSION91_AFTERHOURS_PURE_HELPERS_COVERAGE_ROUND1_2026-09-24.md` — new
  `tests/test_afterhours_scan_pure_helpers.py` (31 tests) covers
  `watchlist_engine/afterhours_scan.py`'s five self-contained helpers
  (`_has_uncontextualized_negative`, `_score_headline`,
  `_parse_item_datetime`, `_is_within_max_age`, `_parse_feed_items`) —
  only `_extract_symbol` (the MANINDS/EKC/OLAELEC/RAYMONDREL/UTLSOLAR
  name-alias fix, already present coming into this round) had direct tests
  before. **Not run through live pytest — no network in this sandbox**;
  written and hand-traced against the real source instead, same caveat as
  sessions 76/77/82c/86. Deferred: the two DB-writing orchestrators
  (`run_afterhours_scan`, `finalize_nextday_watchlist`) and the three
  httpx-calling fetchers. No production code changed.
- `SESSION88_CANDIDATES_COVERAGE_100_PERCENT_2026-09-23.md` —
  100%-coverage plan: `candidate_engine/candidates.py` finished, 65%→100%
  (689/689 statements). New `tests/test_candidates_orchestration.py` (24
  tests, round 3) covers the three DB-writing cycle orchestrators
  session87 deferred (`_refresh_standard_candidates`,
  `_refresh_volume_shock_candidates`, `refresh_candidates`) plus the 6
  stray single-line gaps session87's note called out by number. No
  production-code bugs found — pure coverage-closing pass. Full
  `real-trade-service` suite: 1150 passed, 1 xfailed, no regressions;
  overall repo coverage 78%→80%. `position-stocks-service` re-confirmed
  unchanged (1221 passed).
- `SESSION87_CANDIDATES_COVERAGE_ROUNDS_1_2_2026-09-23.md` — 100%-coverage
  plan follow-up: `candidate_engine/candidates.py`, 0%→65%, both rounds
  actually run through live pytest+coverage (this sandbox had working
  network/pip access). Landed session86's drafted-but-unlanded round 1
  (`tests/test_candidates_helpers.py`, 107 tests — sector-peer-history
  cache, adaptive-param refresh, HTTP fetch wrappers, quality gate, pure
  analysis helpers, source row-normalizers, dedupe-cooldown lookup),
  fixing one real bug the run caught in session86's own draft (a test
  meant to exercise the sector-relative reject path was actually being
  rejected earlier, by the absolute floor, because the floors were never
  lowered from their ~35 default). Added round 2
  (`tests/test_candidates_analysis.py`, 26 tests, new) covering
  `_multi_tf_analysis` and `_volume_shock_analysis` via a routing fake
  `httpx.AsyncClient`; the run caught two more bugs, both in this
  session's own first-draft test fixtures/assertions, not the production
  code. Full suite: 1126 passed, 1 xfailed, no regressions. Deferred to a
  follow-up round: `_refresh_standard_candidates`, `_refresh_volume_shock_
  candidates`, `refresh_candidates` — the DB-writing cycle orchestrators.
- `SESSION86_AUTO_PILOT_ORCHESTRATION_COVERAGE_2026-09-23.md` — 100%-coverage
  plan follow-up: second (and final) coverage round on `execution/
  auto_pilot.py`, targeting the cycle-orchestration layer session85
  deliberately left out. New `tests/test_auto_pilot_orchestration.py` (120
  tests) covers the lock wrappers, `_exit_only_tick_body`, `_full_tick_body`,
  `_select_overnight_holds`'s net-of-costs branch, `_requeue_overnight_
  priority_candidates`, `_inject_nextday_watchlist_candidates`, `_prepick`,
  `_enter_at_open`, `_edis_morning_check`, `_eod_squareoff`, `_eod_signal_
  scan`, `_schedule_tick_body` and all five of its scheduled automations,
  all five background loops, the remaining `_afterhours_scan_body` branches,
  the after-hours lock + manual trigger, and `start()`. Moves `auto_pilot.py`
  31%→~99% (pending VM confirmation). **Not run through live pytest this
  session** (no network in this sandbox, unlike session85's) — verified
  structurally instead: py_compile plus an AST sweep confirming every
  referenced `ap.<name>`, every dotted monkeypatch target, and every model
  kwarg/attribute actually exists in the target module. Same caveat as
  sessions 76/77/82c — user should confirm with a real pytest+coverage run.
  No production code changed, tests only.
- `SESSION85_AUTO_PILOT_HELPERS_COVERAGE_2026-09-23.md` — 100%-coverage plan
  follow-up: first coverage round on `execution/auto_pilot.py`, the largest
  remaining gap (750 stmts). New `tests/test_auto_pilot_helpers.py` (56
  tests) covers the self-contained helpers — locks, reconcile-throttle,
  `_summarize`, overnight-hold/edis gate toggles, `_needs_cnc_sell`,
  gate-off alerting, the afterhours-window/market-date calcs, and the full
  `_select_overnight_holds` eligibility/ranking/cap pipeline. Moves
  `auto_pilot.py` 19%→31%. **Actually executed this session** (sandbox now
  has working pytest + network, unlike prior sessions): full suite run —
  869 passed, 1 xfailed, no regressions; confirms session84's two modules
  and the three previously-100% modules are still 100%. Cycle orchestration
  (`_full_tick_body`, `_prepick`, `_eod_squareoff`, background loops) left
  for a follow-up round; `candidate_engine/candidates.py` (0%, 2077 lines)
  still untouched. No production code changed, tests only.
- `SESSION84_SHARED_ORDER_BUDGET_AND_SYMBOL_LOCK_COVERAGE_2026-09-23.md` —
  100%-coverage plan follow-up: `execution/shared_order_budget.py` (44%→100%)
  and `execution/shared_symbol_lock.py` (41%→100%) closed with 2 new test
  files; confirmed via a real VM pytest run that `exit_engine/exit.py`,
  `portfolio/portfolio.py`, `execution/dhan_client.py` are genuinely 100%;
  flagged `execution/auto_pilot.py` (19%, 611 lines) as the next, largest
  gap and `candidate_engine/candidates.py` (0%, never tested) after that;
  no production code changed, tests only
- `SESSION83_CLAMP_FOR_ATR_IMPORTERROR_COVERAGE_2026-09-21.md` — 100%-coverage
  plan, Phase 1 #1 closed out: `_clamp_for_atr`'s `return_sanity` ImportError
  fallback (the last zero-coverage item session82c flagged) now has 2 direct
  tests; `exit_engine/exit.py` confirmed at 84% coverage via a real pytest run
  (sandbox had pypi egress this session); no new bugs found
- `2026-09-21-session82c-eval-mode-isolation.md` — real bug: `evaluate_mode`'s
  per-position loop had no exception isolation, so one bad position could
  abort stop/target evaluation for every other open position that cycle;
  fixed with try/except + HOLD audit log per position; also added first-ever
  direct tests for `_load_profile`/`_trail_atr_mult`
- `SESSION82_ANGELONE_CROSS_LOOP_LOCK_READTIMEOUT_ROOT_CAUSE_2026-09-21.md` —
  root cause of the ReadTimeout storm: `AngelOneSession`'s single shared
  `asyncio.Lock` bound to whichever event loop touched it first, crashing the
  ws-feed background thread for good on any cross-loop contention; fixed with
  a per-event-loop lock (then a follow-up fix, 82b, to stop it leaking memory
  via one-shot `asyncio.run()` loops using a `WeakKeyDictionary`)
- `SESSION81_STALE_TEST_FIXES_AFTER_SESSION79_80_CHANGES_2026-09-21.md` — 3
  tests updated to match two already-deliberate production changes (session79's
  `MIN_TRADE_VALUE` default drop, session80's dead-exit-leg detection now
  requiring every leg dead, not just one) — no application code changed
- `SESSION77_COVERAGE_PLAN_PHASE1_PART2_2026-09-21.md` — 100%-coverage plan,
  Phase 1 continued: 21 new tests for `exit_engine/exit.py`'s
  CDSL/insufficient-funds/oversell(×3)/exchange-not-allowed branches, both
  `_cutoff_key` siblings (intraday-cutoff, security-intraday-restricted),
  and the generic-rejection streak escalation state machine; no new bugs found
- `SESSION77_COVERAGE_PLAN_PHASE1_PART1_2026-09-21.md` — 100%-coverage plan,
  Phase 1 continued: 12 new tests for `exit_engine/exit.py`'s
  `expire_stale_exit_orders()` and `_send_real_sell`'s success/invalid-IP/
  pre-migration-fallback paths, all previously 0% direct; no new bugs found
- `SESSION76_CIRCUIT_LIMIT_EXIT_RESEND_FIX_2026-09-21.md` — `_send_real_sell`'s
  circuit-limit rejection branch never set the `_cutoff_key` resend-suppression
  flag its sibling branches use, so a circuit-locked position's SELL was
  resent to Dhan every exit cycle all day instead of once; fixed, 2 new tests
- `SESSION75_STUCK_RECONCILE_STATUS_FILTER_FIX_2026-09-20.md` — real bug found
  from live `/reconcile/pending` data: `resolve_stuck_pending()`'s status
  filter silently excluded STOP_HIT/TARGET_HIT rows from ever being
  self-healed or aged-out, so a stale EOD_SQUAREOFF sentinel could sit
  forever on an already-correctly-resolved position; fixed, 4 new tests
- `SESSION74_DEEP_AUDIT_NO_NEW_BUGS_2026-09-20.md` — full (not pattern-swept)
  read of decision-prediction-service's `training/models.py` and
  `training/app.py`, plus a repo-wide sweep for mutable-default-args/bare-except/
  unguarded-division; no new bugs found — remaining open items are config
  decisions, infra, or awaiting live verification, not code
- `SESSION73_CROSS_SERVICE_AUDIT_FIXES_2026-09-20.md` — capital_share_cap
  blind spot to the other service's holdings (new shared-exposure table),
  position-stocks-service exit-placement retry backoff (mirrors
  real-trade-service's session40 fix), overnight-hold sector diversification cap
- `SESSION24_ROOT_DEDUP_CLEANUP.md` — removed root-level duplicates left
  behind by a previous zip repackage (files were already archived but
  never deleted from root)
- 2026-09-11 — quality gate, intraday-restriction list, Dhan P&L summary,
  overnight orchestrator, Reset Failures fix (this session — see the PR/
  commit this shipped in, not yet filed as its own archive note)
- `SESSION23_EOD_SAME_DAY_ENTRY_AND_US_SECTOR_SIGNAL.md`
- `SESSION22_EOD_SQUAREOFF_TIME_AND_OVERNIGHT_SIGNAL_SCAN.md`
- `SESSION21E_LIVE_EVIDENCE_INTRADAY_FIXES.md`
- `SESSION21D_REAL_TRADE_SERVICE_AUDIT.md`
- `SESSION21C_EOD_SQUAREOFF_AND_SELFHEAL_FIXES.md`
- `SESSION21_REAL_TRADE_FIXES.md`
- `OVERSELL_SYNC_IMPORTERROR_FIX.md`
- `INSUFFICIENT_FUNDS_SELL_FIX.md`
- `GATE6_CALIBRATION_AND_CIRCUIT_BREAKER_FIX.md`
- `CDSL_SAME_DAY_EXIT_FIX.md`
- `TICK_SIZE_FLOAT_PRECISION_FIX.md`
- `CLAUDE_SESSION.md`, `CLAUDE_SESSION3.md`

Older notes (pre-Sept 2026) are one level deeper, already archived from a
prior cleanup — same folder, just look for the earlier dates.

Setup/deploy docs stay at repo root, not here: `README.md`,
`DEPLOY_GUIDE.md`, `SETUP-GUIDE.md`, `ORACLE_SETUP_GUIDE.md`.
