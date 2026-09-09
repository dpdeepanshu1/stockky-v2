# Short-Circuit API Waterfall & Render Env Mapping

## Price / OHLCV (market-data-service)

Order — **stop on first success**:

1. Soft / durable cache  
2. Yahoo Finance OHLCV (`yfinance`, `.NS` / `.BO`)  
3. Yahoo `Ticker.info`  
4. **TwelveData** (only if Yahoo failed)  
5. **AlphaVantage** (emergency, ~25/day)  
6. **Polygon** (last resort; sparse India)  
7. Last-good cached quote (never invent zeros)

Helpers: `get_realtime_price()`, `_waterfall_*`, wired inside `GET /quote/{symbol}`.

## Fundamentals (analysis-intelligence-service / fundamental)

1. Market-data → Yahoo `info`  
2. **IndianAPI** only when core fields (PE, ROE, D/E, …) are still empty  

## News & sentiment (analysis-intelligence-service / news)

1. Free RSS (Google News, Moneycontrol, ET, …) via `news_quality` / legacy fetchers  
2. HuggingFace inference for scoring (`HF_API_KEY`)  
3. NewsAPI optional, last  

---

## Render environment variables

### market-data-service

| Variable | Purpose |
|----------|---------|
| `TWELVE_DATA_API_KEY` or `TWELVEDATA_API_KEY` | Primary paid-free price fallback |
| `ALPHA_VANTAGE_API_KEY` | Emergency price only |
| `POLYGON_API_KEY` | Last-resort price |
| `UPSTASH_REDIS_REST_URL` | Cache |
| `UPSTASH_REDIS_REST_TOKEN` | Cache |
| `API_GATEWAY_URL` | Rate-limit event reporting (optional) |

### analysis-intelligence-service

| Variable | Purpose |
|----------|---------|
| `INDIANAPI_KEY` | Fundamental fallback |
| `HF_API_KEY` | Sentiment scoring |
| `NEWSAPI_KEY` | Optional news (last) |
| `MARKET_DATA_URL` | Upstream quotes / fundamentals |
| `UPSTASH_REDIS_REST_URL` / `TOKEN` | Optional |

### decision-prediction-service

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` or `CACHE_DATABASE_URL` | Neon/Postgres models, paper trades, training |
| `MARKET_DATA_URL`, `TECHNICAL_URL`, `FUNDAMENTAL_URL`, `NEWS_URL`, `EVENT_URL` | Downstream services |

**Do not** put all provider keys on the API Gateway — only the service that executes the HTTP call needs the key.

