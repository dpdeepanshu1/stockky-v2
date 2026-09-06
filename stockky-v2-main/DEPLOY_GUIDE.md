# Stockky 5-Service Architecture — Deployment Guide (Separate Render Accounts)

## Overview
Deploy each of the 5 services on **completely separate Render accounts** using the placeholder URLs.
After deployment, perform a global search-and-replace of the placeholder URLs with your real Render URLs.

## Placeholder URLs (use these exactly during initial config)
- https://api-gateway-puwd.onrender.com
- https://market-data-service-r6d7.onrender.com
- https://analysis-intelligence-service.onrender.com
- https://decision-prediction-service.onrender.com
- https://notification-scheduler-service-x8vc.onrender.com/notification

## Prerequisites
- 5 free Render accounts (or paid if preferred)
- Upstash Redis free account (shared across services)
- Optional: CallMeBot for voice alerts
- GitHub repo for the new stockky-v2 code

## Step-by-Step Deployment

### 1. Prepare the repository
Push the entire `stockky-v2` folder contents to a new GitHub repository (or replace main).

### 2. Create Upstash Redis
1. Go to https://upstash.com → create free Redis database
2. Note `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`

### 3. Deploy Market Data Service (Account 1)
1. New Web Service on Render Account 1
2. Connect GitHub repo
3. Root Directory: `services/market-data-service`
4. Build Command: `pip install -r requirements.txt`
5. Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
6. Environment Variables:
   - `UPSTASH_REDIS_REST_URL`
   - `UPSTASH_REDIS_REST_TOKEN`
   - `YFINANCE_MAX_CONCURRENT=6`
7. Note the real URL → replace `STOCKKY-MARKET-DATA` later

### 4. Deploy Analysis Intelligence Service (Account 2)
1. New Web Service on Render Account 2
2. Root Directory: `services/analysis-intelligence-service`
3. Build: `pip install -r requirements.txt`
4. Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Env:
   - `MARKET_DATA_URL=https://YOUR-REAL-MARKET-DATA.onrender.com`
   - `UPSTASH_REDIS_REST_URL` + TOKEN
6. Note real URL

### 5. Deploy Decision Prediction Service (Account 3)
1. Account 3, Root: `services/decision-prediction-service`
2. Same build/start
3. Env:
   - `MARKET_DATA_URL=...`
   - `TECHNICAL_URL=https://YOUR-ANALYSIS.../technical`
   - `FUNDAMENTAL_URL=.../fundamental`
   - `NEWS_URL=.../news`
   - `EVENT_URL=.../event`
   - `UPSTASH_...`
   - Optional: `TRAINING_DATABASE_URL` for persistent Postgres (Neon/Supabase free)

### 6. Deploy Notification Scheduler Service (Account 4)
1. Account 4, Root: `services/notification-scheduler-service`
2. Env: Redis + optional Discord/Slack/Telegram/CallMeBot vars

### 7. Deploy API Gateway (Account 5)
1. Account 5, Root: `services/api-gateway`
2. Env: all the real service URLs pointing to the 4 other services (with /module suffixes where needed)
3. This is the public entry point for the frontend.

### 8. Frontend
- Deploy to Vercel/Netlify
- Set build env `VITE_API_URL=https://YOUR-REAL-API-GATEWAY.onrender.com`

### 9. Global Search & Replace
After all real URLs are known, in the repo:
```bash
# Example
find . -type f \( -name "*.py" -o -name "*.ts" -o -name "*.tsx" -o -name "*.yml" -o -name "*.md" -o -name ".env*" \) \
  -exec sed -i 's|https://market-data-service-r6d7.onrender.com|https://your-real-market-data.onrender.com|g' {} +
# Repeat for each of the 5
```
Then redeploy or push.

### 10. GitHub Actions
Copy `.github/workflows` and update the secrets with the 5 real URLs + wake logic.

### 11. Persistence
For TRAINING_DATABASE_URL use Neon or Supabase free Postgres connection string so training state survives Render restarts.

## Cold-start / Wake pattern
The scheduler and GitHub Actions should first hit `/health` on all 5 services in parallel before any scan.

## Confirmation Checklist
- [ ] All 5 services health endpoints return ok
- [ ] API Gateway can reach analysis and decision modules
- [ ] Market scan returns multi-horizon results
- [ ] Paper trading, CallMeBot, backups work
- [ ] Redis caching active
- [ ] No original functionality lost

## Persistent Database (Neon or Supabase)

Training, paper trades, and T+1/T+5 outcomes need durable storage.

1. Create a free Postgres database:
   - Neon: https://neon.tech
   - Supabase: https://supabase.com
2. Copy the connection string (prefer pooler URL with `sslmode=require`).
3. On **decision-prediction-service** (Render env):
   ```
   DATABASE_URL=postgresql://USER:PASSWORD@HOST/DB?sslmode=require
   ```
4. Redeploy the service. Tables are created via SQLAlchemy `ensure_schema` on startup.
5. Without `DATABASE_URL`, the service falls back to local sqlite (lost on redeploy).

## Placeholder URL replacement checklist

After each service has a real Render URL, set env vars (do not rely on code defaults):

| Service | Key env vars |
|---------|----------------|
| market-data | `UPSTASH_*`, `PORT` |
| analysis-intelligence | `MARKET_DATA_URL`, `UPSTASH_*` |
| decision-prediction | `MARKET_DATA_URL`, `TECHNICAL_URL`, `FUNDAMENTAL_URL`, `NEWS_URL`, `EVENT_URL`, `DATABASE_URL`, `UPSTASH_*` |
| notification-scheduler | `API_GATEWAY_URL`, `UPSTASH_*`, CallMeBot vars |
| api-gateway | all `*_URL` pointing at the four services above, `UPSTASH_*` |
| frontend (build) | `VITE_API_URL=https://YOUR-API-GATEWAY.onrender.com` |

Also update `config/service_urls.py` defaults **or** always inject env so placeholders never leak into production traffic.

## Frontend runtime override

If `VITE_API_URL` was missing at build time, open **Settings** in the app and paste the API Gateway URL once (stored in `localStorage` as `stockky:api_url`).

## Free-tier warmth & alert reliability

### Keep services awake
1. External cron (cron-job.org / GitHub Actions) every **5–10 minutes**:
   ```
   GET https://YOUR-API-GATEWAY/wake-all
   GET https://YOUR-API-GATEWAY/health?warm=true
   GET https://YOUR-MARKET-DATA/health?warm=true
   GET https://YOUR-ANALYSIS/health?warm=true
   GET https://YOUR-DECISION/decision/health?warm=true
   GET https://YOUR-NOTIFICATION/notification/health?warm=true
   ```
2. Gateway `/wake-all` pings all upstream services with `warm=true` and retries failures once.

### Alert outbox
Failed `/notify` deliveries are pushed to Redis key `stockky:notify:outbox` and retried with backoff.
- `GET /notification/outbox` — pending items
- `POST /notification/outbox/process` — process due retries (also called from scheduler before notify)

### Data-quality gate
Decision engine attaches `data_quality` (pillar live counts). BUY NOW / PREPARE are forced to WAIT / DO NOT BUY when price/technical are missing or fewer than 3 pillars are live.

### GitHub Actions keep-warm (required on free tier)
1. Repo → Settings → Secrets → Actions:
   - `API_GATEWAY_URL` = `https://YOUR-api-gateway.onrender.com`
   - Optional: `NOTIFICATION_URL`, `MARKET_DATA_URL`, `ANALYSIS_INTELLIGENCE_URL`, `DECISION_PREDICTION_URL`
2. Enable workflow **Stockky Keep-Warm** (`.github/workflows/wake-services.yml`) — runs every 5 minutes.
3. Market scheduler (`.github/workflows/scheduler.yml`) now calls `/wake-all` (not `/wake/all`) before scans.
4. Local test: `API_GATEWAY_URL=https://... ./scripts/wake_all.sh`
