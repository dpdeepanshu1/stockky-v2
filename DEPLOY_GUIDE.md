# Stockky 5-Service Architecture — Deployment Guide (Separate Render Accounts)

## Overview
Deploy each of the 5 services on **completely separate Render accounts** using the placeholder URLs.
After deployment, perform a global search-and-replace of the placeholder URLs with your real Render URLs.

## Placeholder URLs (use these exactly during initial config)
- https://api-gateway-puwd.onrender.com
- https://market-data-service-r6d7.onrender.com
- https://analysis-intelligence-service.onrender.com
- https://decision-prediction-service.onrender.com
- https://notification-scheduler-service-x8vc.onrender.com

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
