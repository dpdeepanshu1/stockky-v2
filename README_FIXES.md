# Stockky-v2 fixes (Data Feed + cold-start)

Copy these files into your repo **preserving paths**.

## What changed

### 1) Data Feed ↔ GitHub Schedulers
- `scheduler.yml` — after wake, checks `/data-feed/status` and kicks `/data-feed/run` if idle/empty (non-blocking).
- `data-feed-midnight.yml` — full wake of upstream services before feed run.
- `refresh-static-params.yml` — nightly static refresh also kicks Data Feed.

### 2) DB / cache → Data Feed → Upstream fallback
- `services/api-gateway/main.py`
  - `_fetch_fundamental_cached`: Data Feed → Redis → upstream; **write-through** to Data Feed on upstream hit.
  - `_fetch_events_cached`: Redis → Data Feed snapshot → upstream; write-through to Data Feed.
  - New `/ops/keepalive` (GET/POST) — lightweight keep-alive (optional `deep=true`).

### 3) Cold-start / “Request timed out after 15 seconds”
- `frontend/src/api.ts`
  - Default timeout raised; progressive retry + soft-wake on abort/502/503/504.
  - `startSessionKeepAlive` / `stopSessionKeepAlive` — soft ping every ~4.5 min **only while tab is visible**.
- `frontend/src/App.tsx` — starts session keep-alive on mount.
- `wake-services.yml` + `scripts/wake_all.sh` — use `/ops/keepalive`; still market-hours gated (no overload off-hours).

## Apply
```bash
# from stockky-v2 root
cp -r path/to/stockky-fixes/.github ./
cp -r path/to/stockky-fixes/services ./
cp -r path/to/stockky-fixes/frontend ./
cp -r path/to/stockky-fixes/scripts ./
```

Redeploy **api-gateway** + rebuild **frontend**. Ensure GitHub secrets `API_GATEWAY_URL` (and optional service URLs) are set.
