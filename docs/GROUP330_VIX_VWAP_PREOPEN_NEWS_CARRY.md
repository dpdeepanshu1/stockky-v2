# Group 330 - India VIX, VWAP, pre-open snapshot, negative news, carry context

Backend only (plus a pass-through fix in market-data-service). Every new check FAILS OPEN: missing or unreadable data never blocks a trade.
Each has its own env switch (see `.env.example`).

| # | Change | real-trade-service | position-stocks-service |
|---|---|---|---|
| 1 | **India VIX**: no new BUY at VIX >= 24; risk x0.7 at VIX >= 18 or +10 % on the day | `entry_engine/vix_gate.py`, Gate 4d + sizing in `entry.py` | `screening/vix_gate.py`; block inside `trade_gates.market_gate_reject`, size-down in `orders/entry.py` (cache-only read) |
| 2 | **No overnight carry** at VIX >= 20 or +6 % on the day | `_select_overnight_holds` via `entry_engine/carry_context.py` | n/a (intraday) |
| 3 | **VWAP**: no long more than 0.15 % under the day's VWAP | `entry_engine/vwap_gate.py`, `Tick.vwap`, Gate 4d | not built |
| 4 | **09:08 pre-open snapshot**: warms caches, records the indicative gap; gap-up >= 6 % / gap-down >= 3 % is not bought until 11:00 (real-trade) / 10:00 (position-stocks) | `entry_engine/preopen.py`, hook in `_schedule_tick_body` | `screening/preopen.py`, hook in `_trading_loop`, gap check in `attempt_entry` |
| 5 | **Negative news**: event score <= 35, or a regulatory action / delisting risk / fresh earnings miss | `event_risk.news_reason`, Gate 5.8 (same cached event-service call as Gate 5.7) | `QualitySignal.negative_news` in `quality_gate.passes()` (same event answer the gate already fetched) |
| 6 | **Carry context at the close**: Nifty <= -0.8 % vs previous close, VIX high / spiking, negative news -> square off instead of carrying | `entry_engine/carry_context.py` | n/a |

## Fix found on the way
market-data `/quote` and `/quotes/bulk` rows dropped Dhan's `avg_price`, `upper_circuit`, `lower_circuit`, `spread_pct`, `book_value_5`
(`_dhan_quote_payload` / `_pad_quote_response` / `QuoteResponse`). Group 329's circuit gate, the spread re-check and the VWAP gate read those
fields, so they were silently inert. They now pass through when present and positive. Side effect: the spread and book-depth checks start
firing for the first time - watch them in DEMO.

## Not built / honest limits
* Overseas / GIFT-Nifty cues for the carry decision: no verified live source at 15:00 IST. The US-sector snapshot is last night's session and is deliberately not used.
* Real NSE pre-open call-auction data (indicative price / volume): not available through any source verified here. The snapshot uses market-data's latest price
  and counts it only when its `age_s` is within `PREOPEN_MAX_PRICE_AGE_S`; whether the feed has a fresh price at 09:08 is unverified.
* `/quote/INDIAVIX` returning a usable level is unverified; if not, the VIX checks do nothing.
* Position-stocks has no VWAP gate (not requested for it).

## Tests
`real-trade-service/tests/test_group330_gates.py`, `test_group330_wiring.py` (end to end through `evaluate_mode` and `_select_overnight_holds`);
`position-stocks-service/tests/test_group330_gates.py`. Both conftests switch the new gates off for older tests.
**Not run with real pytest in this sandbox (pytest / sqlalchemy / httpx unavailable).** The two pure test files were run through a minimal stand-in runner
(real-trade 35/35, position-stocks 21/22; the one miss was a missing `websockets` package, not the code). `test_group330_wiring.py` and the full
suites need `bash run_tests.sh` on your VM.
