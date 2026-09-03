# Caching: docs vs reality

README mentions “Aggressive Redis.” In code, `USE_REDIS` defaults to **0** in most services.

- **Durable slow store:** Neon Postgres `stockky_kv` via `kv_cache.py`
- **Hot path:** process-local / in-memory TTL
- **Redis (Upstash):** optional when `USE_REDIS=1` and credentials are set

Configure infra around Neon first; enable Redis only if you need shared hot cache across instances.
