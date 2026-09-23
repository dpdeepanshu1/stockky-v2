# Stockky v2 — Changed Files Manifest (Oracle dual-DB + fixes)

This folder contains **every file that changed**, in its correct repo-relative
path. Drop these on top of your repo (both `main` and `backup-production` —
the code is identical on both) and the tree is complete. **First read
`ORACLE_SETUP_GUIDE.md`** (in this folder) for the full Oracle-dashboard → `.env`
walkthrough.

> One file was **deleted**, not changed — a ZIP cannot represent a deletion, so
> do it by hand (see "Deletions" below).

---

## The core idea (why nothing in the code needs to change per environment)

The **same code runs on both databases**. A single environment variable decides
which one:

- **`ORACLE_DSN` empty**  → the app uses **Neon / Postgres** (this is Render, unchanged).
- **`ORACLE_DSN` set**    → the app uses **Oracle Autonomous DB** (this is your VM).

All Postgres/Neon code paths are byte-for-byte the same as before, so **there is
zero risk to your Render deployment.** Oracle support was added as extra branches
that only activate when `ORACLE_DSN` is present. A new shared shim,
`oracle_compat.py`, is the one place that knows how to build an Oracle engine and
emit Oracle-flavoured SQL (MERGE, `VARCHAR2`/`CLOB`, `SYSTIMESTAMP`, `FROM dual`,
etc.); it is deployed identically into all six service folders.

---

## What changed, by area

**Dual-DB engine + portability (new / edited)**
- `oracle_compat.py` — NEW shared shim (identical copy in all 6 service dirs).
- `kv_cache.py` — made fully dialect-aware (identical superset in all 6 dirs):
  durable KV / notification / watchlist tables now work on Postgres **and** Oracle.
- `services/decision-prediction-service/training/models.py` — `get_engine()` picks
  Oracle when configured; Postgres URL handling untouched otherwise.
- `services/decision-prediction-service/training/universe_ingest.py`,
  `.../training/train.py` — engine creation routed through the Oracle-aware path.
- 9 × `requirements.txt` — appended `oracledb==2.5.1` (pure-Python thin-mode
  driver; imported lazily, never loaded on the Neon/Postgres path).

**Graceful skip on Oracle for Postgres-only secondary features (edited)**
- `surprise_premarket.py` (api-gateway + market-data), `surprise_scanner.py`,
  `surprise_schema.py` — the surprise premarket scanner uses Postgres-specific
  SQL; it now cleanly no-ops when `ORACLE_DSN` is set.
- `notification/main.py`, `scheduler/main.py` — the Neon keep-alive `SELECT 1`
  now short-circuits on Oracle (Oracle needs `FROM dual` and has no auto-suspend).

**Infra / deployment (new / edited)**
- `docker-compose.yml` — mounts the Oracle wallet read-only into every backend
  container at `/oracle_wallet` and passes the `ORACLE_*` env through. Fully inert
  on Neon (empty wallet folder, `ORACLE_DSN` unset).
- `.env.oracle.example` — NEW ready-to-fill env template for the Oracle VM.
- `oracle_wallet/.gitkeep` — NEW placeholder so the (git-ignored) wallet folder exists.
- `.gitignore` — NEW rules: never commit the wallet or `.env.oracle`.
- `ORACLE_SETUP_GUIDE.md` — NEW complete setup walkthrough.
- `deploy/nginx-stockky.conf`, `frontend/vite.config.ts`, `frontend/Dockerfile`
  — the 5-second reload fix + preview host/proxy config (Task A).
- `services/api-gateway/main.py`, `services/.../training/app.py`,
  `services/.../training/json_safe.py`, `rate_limiter.py` (×4),
  `redis_rate_limit.py` — backend-log error fixes + safe JSON responses (Task E).

The complete file list is at the bottom of this document.

---

## Deletions (do this by hand — a ZIP can't carry a deletion)

Delete this redundant workflow on **both** branches:

```bash
git rm .github/workflows/market-heartbeat.yml
```

**Why it is safe:** `market-heartbeat.yml` pinged health/docs every 10 minutes
during market hours. `wake-services.yml` already does a *deeper* wake every
**5** minutes in the same window, so the heartbeat added nothing. Removing it
only saves GitHub Actions minutes — no functional loss.

---

## Workflow optimization (Render owns all scheduled jobs, as requested)

All GitHub Actions target **Render** via repo secrets (`API_GATEWAY_URL`, etc.).
They do **not** touch the Oracle VM — your VM runs 24/7 and never spins down, so
it needs none of the "keep-warm" jobs.

| Workflow | Schedule (IST) | Does real work? | Recommendation |
|---|---|---|---|
| ~~market-heartbeat.yml~~ | every 10 min, mkt hrs | No (duplicate) | **DELETED** |
| wake-services.yml | every 5 min, mkt hrs | Keep-warm | **Keep** (primary anti-spindown) |
| neon-keepalive.yml | every 4 min, mkt hrs | Keep-warm (Neon) | **Keep**; optional to disable if you stop using Neon |
| data-feed-midnight.yml | 00:15 | Yes | Keep |
| refresh-static-params.yml | 00:05 | Yes | Keep |
| hot-picks-midnight.yml | 00:30 | Yes | Keep |
| overnight-universe-training.yml | 00:30–05:30 (×4) | Yes (+ evaluate) | Keep |
| retrain-model.yml | 00:30 | Yes | Keep |
| evaluate-outcomes.yml | ~10:00 & ~18:30 | Yes | Keep (daytime T+1/T+5 pass) |
| weekend-hydrator.yml | hourly Sat–Sun | Yes | Keep |
| scheduler.yml | 10:00/12:00/14:00 | Yes | Keep |
| catalyst-alert.yml | 10:15/11:15/13:15/15:15 | Yes | Keep |
| surprise-premarket.yml | 08:55 | Yes | Keep |
| surprise-scanner.yml | hourly, mkt hrs | Yes | Keep |

**Optional further trimming (your call — left intact for safety):**
- Around midnight IST, five jobs fire within 25 minutes (00:05/00:15/00:30 ×3).
  On a free tier you can space them ~15 min apart to reduce contention.
- `overnight-universe-training.yml` already runs a T+1/T+5 evaluate step; its
  overlap with the early `evaluate-outcomes.yml` pass is minor — you may drop the
  duplicate pass if you want fewer runs. Kept both for reliability.

I did **not** auto-delete any of these because they drive your live ML pipeline;
deleting the wrong one would silently break training/scans. The one unambiguous
duplicate (`market-heartbeat.yml`) is the only removal.

---

## Benign warning you can ignore: sklearn / XGBoost model unpickling

On model load you may see a line like:

```
InconsistentVersionWarning: Trying to unpickle estimator ... from version X
when using version Y
```
(or an equivalent XGBoost "loaded from older version" note).

**This is harmless.** It only means the saved model file was created with a
slightly different library version than the one now installed. Predictions still
work. To make it disappear, simply let the next scheduled **retrain** run
(`retrain-model.yml`) re-save the model with the current version — no action
needed on your side.

---

## Complete list of changed files (46)

```
.env.oracle.example
.gitignore
ORACLE_SETUP_GUIDE.md
docker-compose.yml
oracle_wallet/.gitkeep
deploy/nginx-stockky.conf
frontend/Dockerfile
frontend/vite.config.ts
services/analysis-intelligence-service/requirements.txt
services/analysis-intelligence-service/fundamental/kv_cache.py
services/analysis-intelligence-service/fundamental/oracle_compat.py
services/analysis-intelligence-service/fundamental/rate_limiter.py
services/analysis-intelligence-service/fundamental/requirements.txt
services/api-gateway/kv_cache.py
services/api-gateway/main.py
services/api-gateway/oracle_compat.py
services/api-gateway/rate_limiter.py
services/api-gateway/redis_rate_limit.py
services/api-gateway/requirements.txt
services/api-gateway/surprise_premarket.py
services/api-gateway/surprise_scanner.py
services/api-gateway/surprise_schema.py
services/decision-prediction-service/requirements.txt
services/decision-prediction-service/decision/kv_cache.py
services/decision-prediction-service/decision/oracle_compat.py
services/decision-prediction-service/decision/requirements.txt
services/decision-prediction-service/training/app.py
services/decision-prediction-service/training/json_safe.py
services/decision-prediction-service/training/kv_cache.py
services/decision-prediction-service/training/models.py
services/decision-prediction-service/training/oracle_compat.py
services/decision-prediction-service/training/requirements.txt
services/decision-prediction-service/training/train.py
services/decision-prediction-service/training/universe_ingest.py
services/market-data-service/kv_cache.py
services/market-data-service/oracle_compat.py
services/market-data-service/rate_limiter.py
services/market-data-service/requirements.txt
services/market-data-service/surprise_premarket.py
services/notification-scheduler-service/requirements.txt
services/notification-scheduler-service/notification/kv_cache.py
services/notification-scheduler-service/notification/main.py
services/notification-scheduler-service/notification/oracle_compat.py
services/notification-scheduler-service/notification/requirements.txt
services/notification-scheduler-service/scheduler/main.py
services/notification-scheduler-service/scheduler/rate_limiter.py
```

Deleted (manual): `.github/workflows/market-heartbeat.yml`
