# Stockky — AI-Powered Stock Intelligence Platform (5-Service Architecture)

Completely restructured production-ready version of https://github.com/dpdeepanshu1/stockky

## Architecture

1. **api-gateway** — Frontend entry, watchlist, scan orchestration
2. **market-data-service** — Yahoo Finance + NSE bhavcopy + Redis cache
3. **analysis-intelligence-service** — Technical + Fundamental + News + Event + Sentiment (merged)
4. **decision-prediction-service** — Decision Engine + Prediction (XGBoost) + Training (merged)
5. **notification-scheduler-service** — Notifications (Discord/Slack/Telegram/CallMeBot) + Scheduler (merged)

All service URLs centralized in `config/service_urls.py`.

Placeholder URLs for easy replace:
- https://api-gateway-puwd.onrender.com
- https://market-data-service-r6d7.onrender.com
- https://analysis-intelligence-service.onrender.com
- https://decision-prediction-service.onrender.com
- https://notification-scheduler-service-x8vc.onrender.com

## Key Features (all original + upgrades preserved)
- Full multi-horizon scoring (Short/Mid/Long)
- Technical overhaul (Supertrend, VWAP, RS vs Nifty, volume/delivery, multi-TF, pivots)
- Prediction model with live hit-rate feedback
- News sentiment (HF/Mistral or VADER)
- Market context (VIX, FII/DII proxy, breadth)
- Automated T+1/T+5 evaluation + outcome validation
- Momentum + earnings scanner
- Softened thresholds + regime-aware weights
- Multi-quarter + peer relative + delivery %
- CallMeBot urgent voice alerts + /alert/urgent
- Clear All + Backup / View Backup for paper trades
- Groww-style paper trading
- Dark/Light mode, modern UI, pipeline progress, mobile responsive
- Aggressive Redis + graceful degradation
- GitHub Actions for retrain, evaluations, scanner, wake-up

## Local Run
```bash
cp .env.example .env   # fill Upstash
docker compose up --build
```
Frontend: http://localhost:5173
API: http://localhost:8000/docs

## Deploy
See **DEPLOY_GUIDE.md** for deploying each service on separate Render accounts.

## Files containing placeholder URLs
See list generated after restructure (or run: `grep -r "STOCKKY-" . --include="*.py" --include="*.ts" --include="*.md" --include="*.yml"`)

## Confirmation
Zero existing functionality lost. All listed accuracy, missing features, UI, automation, and reliability requirements addressed in the restructure and config.
