# Speed / Infra Tuning — Oracle Cloud VM (2 vCPU / 12 GB) — 2026-09-02

Scope: infrastructure and deployment only. No changes to candidate_engine,
entry_engine, exit_engine, risk_engine, training pipeline, or any
scan-concurrency / cache-TTL / rate-limit constant — those are calibrated
against Yahoo/NSE/AngelOne behavior and data freshness, not local hardware,
and were left untouched on purpose.

## Files changed
- `docker-compose.yml` — added `cpus:` / `mem_limit:` / `mem_reservation:`
  per service (sized for 2 vCPU / 12 GB total, ~5 GB RAM budget, leaves
  headroom for OS + Oracle wallet TLS + Docker itself), and a shared
  `json-file` logging cap (`max-size: 10m`, `max-file: 3`) on every
  service so unbounded container logs can't slowly fill the disk.
- `frontend/Dockerfile` — now a 2-stage build: Node only builds the static
  bundle; it's served by `nginx:1.27-alpine`, not `vite preview` (a Node
  process). Same bundle, far less RAM, faster first byte.
- `frontend/nginx.conf` — new. gzip + 1-year immutable cache headers on
  hashed JS/CSS/font assets; `index.html` explicitly never cached; SPA
  fallback to `index.html` for client-side routes.
- `deploy/nginx-stockky.conf` (host-level, outside Docker) — added
  `http2` to the TLS listener, gzip, and `keepalive` upstream pools for
  the frontend/api-gateway/real-trade backends. All existing routes,
  timeouts, and the `/realtrade/` trust boundary are byte-for-byte
  unchanged from the version that already fixed the 504s.
- `scripts/oracle-vm-tune.sh` — new, one-time host setup: 4 GB swap file
  + `vm.swappiness=10` (safety net against an OOM-kill during a training
  burst; invisible in normal operation) and a Docker daemon-level log
  rotation default as a backstop.
- `.env.oracle.recommended` — new, NOT applied automatically. A fully
  annotated copy of `.env.oracle.example` with the Oracle connection-pool
  sizes tuned down (see below) and everything else left at the repo's
  existing tuned defaults. Diff it against your real `.env` and merge by
  hand — it still has `<<< FILL IN >>>` placeholders for your actual
  Oracle/AngelOne/Dhan credentials.

## The one real correctness-adjacent finding
Each service opens **two** separate Oracle connection pools: the main DB
engine (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`, default 3+2=5) and a *separate*
engine for the KV-cache's durability table (`CACHE_DB_POOL_SIZE_ORACLE` +
`CACHE_DB_MAX_OVERFLOW_ORACLE`, default 5+3=8 on the Oracle path). Across
six services that's a worst-case of ~78 Oracle sessions, which risks
`ORA-12520: no more sessions` under load on a small Autonomous DB shape.
The in-process memory cache in front of it already serves nearly every
read, so `.env.oracle.recommended` shrinks both pools to 2+1 per engine —
this only reduces idle pooled connections, it does not change what gets
read, written, or how fresh it is.

## Not changed (left alone on purpose)
`MAX_PARALLEL_SCAN_WORKERS`, `YFINANCE_MAX_CONCURRENT`,
`YFINANCE_MIN_INTERVAL_SEC`, `DECIDE_CACHE_TTL_*`, `WATCHLIST_SCAN_*`,
and every data-fetch/waterfall-fallback code path.
